'''
Created on 19.03.2018

@author: mschroeder
'''
import datetime
from distutils.util import strtobool

import numpy as np
from flask import jsonify as flask_jsonify, request
from flask.blueprints import Blueprint
from sklearn.manifold.isomap import Isomap

from morphocluster.tree import Tree
import warnings
from morphocluster.classifier import Classifier
from functools import wraps
import json
from flask import Response
import uuid
import zlib
from redis.exceptions import RedisError
from morphocluster import models
from morphocluster.extensions import database, redis_store
from pprint import pprint
from flask.helpers import url_for
from flask_restful import reqparse
from morphocluster.helpers import seq2array, keydefaultdict
from timer_cm import Timer


api = Blueprint("api", __name__)


def log(connection, action, node_id = None, reverse_action = None):
    auth = request.authorization
    username = auth.username if auth is not None else None
    
    stmt = models.log.insert({'node_id': node_id,
                              'username': username,
                              'action': action,
                              'reverse_action': reverse_action})
    
    connection.execute(stmt)


@api.record
def record(state):
    api.config = state.app.config

@api.after_request
def no_cache_header(response):
    response.headers['Last-Modified'] = datetime.datetime.now()
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response

def _node_icon(node):
    if node["starred"]:
        return "mdi mdi-star"
    
    if node["approved"]:
        return "mdi mdi-approval"
    
    return "mdi mdi-hexagon-multiple"

#===============================================================================
# /tree
#===============================================================================
def _tree_root(project):
    project["text"] = project["name"]
    project["children"] = True
    project["icon"] = "mdi mdi-tree"
    project["id"] = project["node_id"]
    
    return project

def _tree_node(node, supertree=False):
    result = {
        "id": node["node_id"],
        "text": "{} ({})".format(node["name"] or node["node_id"], node["_n_children"]),
        "children": node["n_superchildren"] > 0 if supertree else node["_n_children"] > 0,
        "icon": _node_icon(node)
    }
    
    return result

@api.route("/tree", methods=["GET"])
def get_tree_root():
    with database.engine.connect() as connection:
        tree = Tree(connection)
        result = [_tree_root(p) for p in tree.get_projects()]
        
        return jsonify(result)
    
@api.route("/tree/<int:node_id>", methods=["GET"])
def get_subtree(node_id):
    flags = {k: request.args.get(k, 0, strtobool) for k in ("supertree",)}
    
    with database.engine.connect() as connection:
        tree = Tree(connection)
        
        if flags["supertree"]:
            children = tree.get_children(node_id, supertree=True, include="starred", order_by="_n_children DESC")
        else:
            children = tree.get_children(node_id, order_by="_n_children DESC")
            
        result = [_tree_node(c, flags["supertree"]) for c in children]
        
        return jsonify(result)
    

#===============================================================================
# /projects
#===============================================================================



@api.route("/projects", methods=["GET"])
def get_projects():
    with database.engine.connect() as connection:
        tree = Tree(connection)
        return jsonify(tree.get_projects())

#===============================================================================
# /nodes
#===============================================================================

@api.route("/nodes", methods=["POST"])
def create_node():
    """
    Create a new node.
    
    Request parameters:
        project_id
        name
        members
        starred
    """
    
    with database.engine.connect() as connection:
        tree = Tree(connection)
        data = request.get_json()
        
        object_ids = [m["object_id"] for m in data["members"] if "object_id" in m]
        node_ids = [m["node_id"] for m in data["members"] if "node_id" in m]
    
        project_id = data.get("project_id", None)
        name = data.get("name", None)
        parent_id = int(data.get("parent_id"))
        
        starred = strtobool(str(data.get("starred", "0")))
        
        if project_id is None:
            # Retrieve project_id for the parent_id
            project_id = tree.get_node(parent_id)["project_id"]
        
        print(data)
        
        with connection.begin():
            node_id = tree.create_node(int(project_id), parent_id = parent_id, name = name, starred = starred)
            
            tree.relocate_nodes(node_ids, node_id)
            
            tree.relocate_objects(object_ids, node_id)
            
            log(connection, "create_node", node_id = node_id)
                
            node = tree.get_node(node_id, require_valid=True)
           
        result = _node(tree, node)
         
        return jsonify(result)


def _node(tree, node, include_children=False):
    if node["name"] is None:
        node["name"] = node["orig_id"]
    
    result = {
        "node_id": node["node_id"],
        "id": node["node_id"],
        "path": tree.get_path_ids(node["node_id"]),
        "text": "{} ({})".format(node["name"], node["_n_children"]),
        "name": node["name"],
        "children": node["_n_children"] > 0,
        "n_children": node["_n_children"],
        "icon": _node_icon(node),
        "type_objects": node["_type_objects"],
        "starred": node["starred"],
        "approved": node["approved"],
        "own_type_objects": node["_own_type_objects"],
        "n_objects_deep": node["_n_objects_deep"] or 0,
    }
    
    if include_children:
        result["children"] = [_node(tree, c) for c in tree.get_children(node["node_id"])]
    
    return result

def _object(object_):
    return {"object_id": object_["object_id"]}


def _arrange_by_sim(result):
    """
    Return empty tuple for unchanged order.
    """
    ISOMAP_FIT_SUBSAMPLE_N = 1000
    ISOMAP_N_NEIGHBORS = 5
    
    if len(result) <= ISOMAP_N_NEIGHBORS:
        return ()
    
    # Get vector values
    vectors = seq2array([ m["_centroid"] if "_centroid" in m else m["vector"] for m in result ],
                        len(result))
            
    if vectors.shape[0] <= ISOMAP_FIT_SUBSAMPLE_N:
        subsample = vectors
    else:
        idxs = np.random.choice(vectors.shape[0], ISOMAP_FIT_SUBSAMPLE_N, replace=False)
        subsample = vectors[idxs]
    
    try:
        isomap = Isomap(n_components=1, n_neighbors=ISOMAP_N_NEIGHBORS, n_jobs=4).fit(subsample)
        order = np.squeeze(isomap.transform(vectors))
    except ValueError:
        print(subsample)
        raise
    
    order = np.argsort(order)
        
    return order


def _arrange_by_nleaves(result):
    n_leaves = np.array([ len(m["_leaves"]) if "_leaves" in m else 0 for m in result ],
                        dtype = int)
    
    return np.argsort(n_leaves)[::-1]


def _members(tree, members):
    return [_node(tree, m) if "node_id" in m else _object(m) for m in members]

def batch(iterable, n=1):
    """
    Taken from https://stackoverflow.com/a/8290508/1116842
    """
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]


def json_dumps(o, *args, **kwargs):
    try:
        return json.dumps(o, *args, **kwargs)
    except TypeError:
        pprint(o)
        raise
    
def jsonify(*args, **kwargs):
    try:
        return flask_jsonify(*args, **kwargs)
    except TypeError:
        pprint(args)
        pprint(kwargs)
        raise

def _load_or_calc(func, func_kwargs, request_id, page, page_size = 100, compress = True):
    cache_key = '{}:{}:{}'.format(func.__name__,
                                  json_dumps(func_kwargs, sort_keys = True, separators=(',', ':')),
                                  request_id)
    
    print("Load or calc {}...".format(cache_key))
    
    try:
        page_result = redis_store.lindex(cache_key, page)
    
        if page_result is not None:
            n_pages = redis_store.llen(cache_key)
            
            if compress:
                page_result = zlib.decompress(page_result)
                
            #print("Returning page {} from cached result".format(page))
            
            return page_result, n_pages
        
    except RedisError as e:
        warnings.warn("RedisError: {}".format(e))
        
    # Calculate result
    result = func(**func_kwargs)
    
    # Paginate full_result
    pages = batch(result, page_size)
    
    # Serialize individual pages
    pages = [json_dumps(p) for p in pages]
    
    n_pages = len(pages)
    
    if n_pages:
        if compress:
            #raw_length = sum(len(p) for p in pages)
            cache_pages = [zlib.compress(p.encode()) for p in pages]
            #compressed_length = sum(len(p) for p in pages)
            
            #print("Compressed pages. Ratio: {:.2%}".format(compressed_length / raw_length))
        else:
            cache_pages = pages
            
        try:
            redis_store.rpush(cache_key, *cache_pages)
        except RedisError as e:
            warnings.warn("RedisError: {}".format(e))
    
    if 0 <= page < n_pages:
        return pages[page], n_pages
    
    return "[]", n_pages
    

def cache_serialize_page(endpoint, **kwargs):
    """
    `func` is expected to return a json-serializable list.
    It gains the `page` and `request_id` parameter. The resulting list is split into batches of `page_size` items.
    
    Decorated Function:
        func: func(**kwargs) -> list
        
        ! func is expected to only take keyword parameters !
        
    Return:
        Response()
    
    Example:
        @cache_serialize_page()
        def foo():
            return ["a", "b", "c"]
            
        foo(page=0) -> "a", True
    """
    
    def decorator(func):
        @wraps(func)
        def wrapper(page = None, request_id = None, **func_kwargs):
            if page is None:
                raise ValueError("page may not be None!")
            
            result, n_pages = _load_or_calc(func, func_kwargs, request_id, page, **kwargs)
            
            #===================================================================
            # Construct response
            #===================================================================
            response = Response(result, mimetype=api.config['JSONIFY_MIMETYPE'])
            
            #=======================================================================
            # Generate Link response header
            #=======================================================================
            link_header_fields = []
            link_parameters = func_kwargs.copy()
            link_parameters["request_id"] = request_id
            
            if 0 < page < n_pages:
                # Link to previous page
                link_parameters["page"] = page - 1
                url = url_for(endpoint, **link_parameters)
                link_header_fields.append('<{}>; rel="previous"'.format(url))
            
            
            if page + 1 < n_pages:
                # Link to next page
                link_parameters["page"] = page + 1
                url = url_for(endpoint, **link_parameters)
                link_header_fields.append('<{}>; rel="next"'.format(url))
                
            # Link to last page
            link_parameters["page"] = n_pages - 1
            url = url_for(endpoint, **link_parameters)
            link_header_fields.append('<{}>; rel="last"'.format(url))
            
            response.headers["Link"] = ",". join(link_header_fields)
                        
            return response
            
        return wrapper
    
    return decorator    
    
def _arrange_by_starred_sim(result, starred):
    if len(starred) == 0:
        return _arrange_by_sim(result)
    
    if len(result) == 0:
        return ()
    
    try:
        # Get vectors
        vectors = seq2array((m["_centroid"] if "_centroid" in m else m["vector"] for m in result),
                            len(result))
        starred_vectors = seq2array((m["_centroid"] for m in starred),
                                    len(starred))
    except ValueError as e:
        print(e)
        return ()

    try:
        classifier = Classifier(starred_vectors)
        distances = classifier.distances(vectors)
        max_dist = np.max(distances, axis=0)
        max_dist_idx = np.argsort(max_dist)[::-1]
        
        assert len(max_dist_idx) == len(result), "{} != {}".format(len(max_dist_idx), len(result))
        
        return max_dist_idx
        
    except:
        print("starred_vectors", starred_vectors.shape)
        print("vectors", vectors.shape)
        raise


@cache_serialize_page(".get_node_members")
def _get_node_members(node_id, nodes = False, objects = False, arrange_by = "", starred_first = False):
    with database.engine.connect() as connection, Timer("_get_node_members") as timer:
        tree = Tree(connection)
        
        sorted_nodes_include = "unstarred" if starred_first else None
        
        result = []
        if nodes:
            with timer.child("tree.get_children()"):
                result.extend(tree.get_children(node_id, include=sorted_nodes_include))
        if objects:
            with timer.child("tree.get_objects()"):
                result.extend(tree.get_objects(node_id))
            
        if arrange_by == "starred_sim" or starred_first:
            with timer.child("tree.get_children(starred)"):
                starred = tree.get_children(node_id, include="starred")
            
        if arrange_by != "":
            result = np.array(result, dtype=object)
            
            if arrange_by == "sim":
                with timer.child("sim"):
                    order = _arrange_by_sim(result)
            elif arrange_by == "nleaves":
                with timer.child("nleaves"):
                    order = _arrange_by_nleaves(result)
            elif arrange_by == "starred_sim":
                with timer.child("starred_sim"):
                    # If no starred members yet, arrange by distance to regular children
                    anchors = starred if len(starred) else tree.get_children(node_id)
                    
                    order = _arrange_by_starred_sim(result, anchors)
            elif arrange_by == "interleaved":
                with timer.child("interleaved"):
                    order = _arrange_by_sim(result)
                    if len(order):
                        order0, order1 = np.array_split(order.copy(), 2)
                        order[::2] = order0
                        order[1::2] = order1[::-1]
            else:
                warnings.warn("arrange_by={} not supported!".format(arrange_by))
                order = ()
                
            #===================================================================
            # if len(order):
            #     try:
            #         assert np.all(np.bincount(order) == 1)
            #     except:
            #         print(order)
            #         print(np.bincount(order))
            #         raise
            #===================================================================
            
            result = result[order].tolist()
            
        if starred_first:
            result = starred + result
            
        result = _members(tree, result)
    
        return result


@api.route("/nodes/<int:node_id>/members", methods=["GET"])
def get_node_members(node_id):
    """
    Provide a collection of objects and/or children.
    
    URL parameters:
        node_id (int): ID of a node
        
    Request parameters:
        nodes (boolean): Include nodes in the response?
        objects (boolean): Include objects in the response?
        arrange_by ("sim"|"nleaves"): Arrange members by similarity / number of leaves / ...
        page (int): Page number (default 0)
        request_id (str, optional): Identification string for the current request collection.
        starred_first (boolean): Return starred children first (default: 0)
    
    Returns:
        List of members
    """
    
    parser = reqparse.RequestParser()
    parser.add_argument("nodes", type=strtobool, default = 0)
    parser.add_argument("objects", type=strtobool, default = 0)
    parser.add_argument("arrange_by", default = "")
    parser.add_argument("page", type = int, default = 0)
    parser.add_argument("request_id")
    parser.add_argument("starred_first", type=strtobool, default = 1)
    arguments = parser.parse_args(strict=True)
    
    if arguments.request_id is None:
        arguments.request_id = uuid.uuid4().hex
    
    return _get_node_members(node_id = node_id, **arguments)
    
@api.route("/nodes/<int:node_id>/members", methods=["POST"])
def post_node_members(node_id):
    data = request.get_json()
    
    object_ids = [d["object_id"] for d in data if "object_id" in d]
    node_ids = [d["node_id"] for d in data if "node_id" in d]
    
    print("new nodes:", node_ids)
    print("new objects:", object_ids)
    
    with database.engine.connect() as connection:
        tree = Tree(connection)

        with connection.begin():
            tree.relocate_nodes(node_ids, node_id)
            tree.relocate_objects(object_ids, node_id)
    
    return jsonify("ok")


@api.route("/nodes/<int:node_id>", methods=["GET"])
def get_node(node_id):    
    with database.engine.connect() as connection:
        tree = Tree(connection)
        
        flags = {k: request.args.get(k, 0, strtobool) for k in ("include_children",)}
        
        node = tree.get_node(node_id)
        
        log(connection, "get_node", node_id = node_id)
        
        result = _node(tree, node, **flags)
        
        return jsonify(result)
    
    
@api.route("/nodes/<int:node_id>", methods=["PATCH"])
def patch_node(node_id):
    with database.engine.connect() as connection:
        tree = Tree(connection)
        
        data = request.get_json()
        flags = {k: request.args.get(k, 0, strtobool) for k in ("include_children",)}
        
        # TODO: Use argparse
        if "starred" in data:
            data["starred"] = strtobool(str(data["starred"]))
            
        if "parent_id" in data:
            raise ValueError("parent_id must not be set directly, use /nodes/<node_id>/adopt.")
        
        with connection.begin():
            tree.update_node(node_id, data)
            
            log(connection,
                "update_node({})".format(json.dumps(data, sort_keys=True)),
                node_id = node_id)
            
            node = tree.get_node(node_id, True)
        
        result = _node(tree, node, **flags)
        
        return jsonify(result)
    
@api.route("/nodes/<int:parent_id>/adopt_members", methods=["POST"])
def node_adopt_members(parent_id):
    """
    Adopt a list of nodes.
    
    URL parameters:
        parent_id (int): ID of the node that accepts new members.
        
    Request parameters:
        members: List of nodes ({node_id: ...}) and objects ({object_id: ...}).
    
    Returns:
        Nothing.
    """
    with database.engine.connect() as connection:
        tree = Tree(connection)
        
        members = request.get_json()["members"]
        
        node_ids = [int(m["node_id"]) for m in members if "node_id" in m]
        object_ids = [m["object_id"] for m in members if "object_id" in m]
        
        with connection.begin():
            tree.relocate_nodes(node_ids, parent_id)
            tree.relocate_objects(object_ids, parent_id)
        
        return jsonify({})
        
        
@cache_serialize_page(".node_get_recommended_children", page_size=20)
def _node_get_recommended_children(node_id, max_n):
    with database.engine.connect() as connection:
        tree = Tree(connection)
        result = [ _node(tree, c) for c in tree.recommend_children(node_id, max_n=max_n) ]
        return result

@api.route("/nodes/<int:node_id>/recommended_children", methods=["GET"])
def node_get_recommended_children(node_id):
    """
    Recommend children for this node.
    
    URL parameters:
        node_id (int): ID of the node.
        
    Request parameters (GET):
        page (int): Page number (default 0)
        request_id (str, optional): Identification string for the current request collection.
    """
    parser = reqparse.RequestParser()
    parser.add_argument("page", type = int, default = 0)
    parser.add_argument("max_n", type = int, default = 100)
    parser.add_argument("request_id", default = lambda: uuid.uuid4().hex)
    arguments = parser.parse_args(strict=True)
    
    # Limit max_n
    arguments.max_n = max(arguments.max_n, 1000)
    
    return _node_get_recommended_children(node_id = node_id, **arguments)

@cache_serialize_page(".node_get_recommended_objects", page_size=20)
def _node_get_recommended_objects(node_id, max_n):
    with database.engine.connect() as connection:
        tree = Tree(connection)
    
        result = [ _object(o) for o in tree.recommend_objects(node_id) ]
        
        return result
    
@api.route("/nodes/<int:node_id>/recommended_objects", methods=["GET"])
def node_get_recommended_objects(node_id):
    """
    Recommend objects for this node.
    
    URL parameters:
        node_id (int): ID of the node.
        
    Request parameters (GET):
        page (int): Page number (default 0)
        request_id (str, optional): Identification string for the current request collection.
        max_n (int): Maximum number of recommended objects.
    """
    parser = reqparse.RequestParser()
    parser.add_argument("page", type = int, default = 0)
    parser.add_argument("max_n", type = int, default = 100)
    parser.add_argument("request_id", default = lambda: uuid.uuid4().hex)
    arguments = parser.parse_args(strict=True)
    
    # Limit max_n
    arguments.max_n = max(arguments.max_n, 1000)
    
    return _node_get_recommended_objects(node_id = node_id, **arguments)

    
@api.route("/nodes/<int:node_id>/tip", methods=["GET"])
def node_get_tip(node_id):
    with database.engine.connect() as connection:
        tree = Tree(connection)
    
        return jsonify(tree.get_tip(node_id))
    
@api.route("/nodes/<int:node_id>/next", methods=["GET"])
def node_get_next(node_id):
    with database.engine.connect() as connection:
        tree = Tree(connection)
    
        return jsonify(tree.get_next_unapproved(node_id))
    
    
@api.route("/nodes/<int:node_id>/n_sorted", methods=["GET"])
def node_get_n_sorted(node_id):
    with database.engine.connect() as connection:
        tree = Tree(connection)
        
        nodes = tree.get_minlevel_starred(node_id)
        
        n_sorted = sum(n["_n_objects_deep"] for n in nodes)
        
        return jsonify(n_sorted)
    
    
@api.route("/nodes/<int:node_id>/merge_into", methods=["POST"])
def post_node_merge_into(node_id):
    """
    Merge a node into another node.
    
    URL parameters:
        node_id: Node that is merged.
        
    Request parameters:
        dest_node_id: Node that absorbs the children and objects.
    """
    with database.engine.connect() as connection:
        tree = Tree(connection)
        
        data = request.get_json()
        
        # TODO: Unapprove
        tree.merge_node_into(node_id, data["dest_node_id"])
        
        log(connection, "merge_node_into({}, {})".format(node_id, data["dest_node_id"]),
            node_id = data["dest_node_id"])
        
        return jsonify(None)
    
@api.route("/nodes/<int:node_id>/classify", methods=["POST"])
def post_node_classify(node_id):
    """
    Classify the members of a node into their starred siblings.
    
    URL parameters:
        node_id: Parent of the classified members.
        
    GET parameters:
        nodes (boolean): Classify nodes? (Default: False)
        objects (boolean): Classify objects? (Default: False)
        safe (boolean): Perform safe classification (Default: False)
        subnode (boolean): Move classified objects into a child of the target node. (Default: False)
    """
    
    flags = {k: request.args.get(k, 0, strtobool) for k in ("nodes","objects","safe","subnode")}
    
    print(flags)
    
    n_predicted_children = 0
    n_predicted_objects = 0
    
    with database.engine.connect() as connection:
        tree = Tree(connection)
        
        # Split children into starred and unstarred
        with connection.begin():
            children = tree.get_children(node_id)
            
            starred = []
            unstarred = []
            for c in children:
                (starred if c["starred"] else unstarred).append(c)
                
            starred_centroids = np.array([c["_centroid"] for c in starred])
            
            print("|starred_centroids|", np.linalg.norm(starred_centroids, axis=1))
            
            # Initialize classifier
            classifier = Classifier(starred_centroids)
            
            if flags["subnode"]:
                def _subnode_for(node_id):
                    return tree.create_node(parent_id=node_id, name="classified")
                target_nodes = keydefaultdict(_subnode_for) 
            else:
                target_nodes = keydefaultdict(lambda k: k) 
            
            if flags["nodes"]:
                unstarred_centroids = np.array([c["_centroid"] for c in unstarred])
                unstarred_ids = np.array([c["node_id"] for c in unstarred])
                
                # Predict unstarred children (if any)
                n_unstarred = len(unstarred_centroids)
                if n_unstarred > 0:
                    print("Predicting {} unstarred children of {}...".format(n_unstarred, node_id))
                    type_predicted = classifier.classify(unstarred_centroids, safe=flags["safe"])
                    
                    for i, starred_node in enumerate(starred):
                        nodes_to_move = [int(n) for n in unstarred_ids[type_predicted == i]]
                        
                        if len(nodes_to_move):
                            target_node_id = target_nodes[starred_node["node_id"]]
                            tree.relocate_nodes(nodes_to_move,
                                                target_node_id,
                                                unapprove=True)
                        
                    n_predicted_children = np.sum(type_predicted > -1)
            
            if flags["objects"]:
                #Predict objects
                objects = tree.get_objects(node_id)
                print("Predicting {} objects of {}...".format(len(objects), node_id))
                object_vectors = np.array([o["vector"] for o in objects])
                object_ids = np.array([o["object_id"] for o in objects])
                
                type_predicted = classifier.classify(object_vectors, safe=flags["safe"])
                
                for i, starred_node in enumerate(starred):
                    objects_to_move = [str(o) for o in object_ids[type_predicted == i]]
                    if len(objects_to_move):
                        target_node_id = target_nodes[starred_node["node_id"]]
                        print("Moving objects {!r} -> {}".format(objects_to_move, target_node_id))
                        tree.relocate_objects(objects_to_move,
                                              target_node_id,
                                              unapprove=True)
                    
                n_predicted_objects = np.sum(type_predicted > -1)
            
            log(connection, "classify_members(nodes={nodes},objects={objects})".format(**flags), node_id = node_id)
            
            return jsonify({"n_predicted_children": int(n_predicted_children),
                            "n_predicted_objects": int(n_predicted_objects)})
    