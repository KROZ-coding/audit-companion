from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import get_store, require_roles
from ..models import User
from ..schemas.api import GraphMappingRequest, GraphNodeCreate, GraphNodeOut
from ..store import Store

router = APIRouter(prefix="/api/graph", tags=["graph"])


@router.get("/nodes", response_model=list[GraphNodeOut])
def list_nodes(graph: Literal["knowledge", "comp", "problem"] | None = None, user: User = Depends(require_roles("student", "teacher", "admin")), store: Store = Depends(get_store)) -> list[GraphNodeOut]:
    return [
        GraphNodeOut(**node) for node in store.graph_nodes.values()
        if (graph is None or node["graph"] == graph)
        and (user.role != "teacher" or node.get("created_by") == user.id)
    ]


@router.post("/nodes", response_model=GraphNodeOut, status_code=201)
def create_node(payload: GraphNodeCreate, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> GraphNodeOut:
    _validate_mapping(payload, store, user)
    node = {"id": str(uuid4()), "created_by": user.id, **payload.model_dump()}
    with store.lock:
        store.graph_nodes[node["id"]] = node
    store.audit("graph_node_create", user.id, {"node_id": node["id"]})
    return GraphNodeOut(**node)


@router.put("/nodes/{node_id}", response_model=GraphNodeOut)
def update_node(node_id: str, payload: GraphNodeCreate, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> GraphNodeOut:
    node = _get_node(node_id, store)
    _check_node_access(node, user)
    _validate_mapping(payload, store, user, node_id=node_id)
    with store.lock:
        store.graph_nodes[node_id] = {"id": node_id, "created_by": node.get("created_by"), **payload.model_dump()}
    store.audit("graph_node_update", user.id, {"node_id": node_id})
    return GraphNodeOut(**store.graph_nodes[node_id])


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(node_id: str, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> None:
    node = _get_node(node_id, store)
    _check_node_access(node, user)
    with store.lock:
        del store.graph_nodes[node_id]
        for node in store.graph_nodes.values():
            if node.get("map_from") == node_id:
                node["map_from"] = None
    store.audit("graph_node_delete", user.id, {"node_id": node_id})


@router.put("/mapping")
def update_mapping(payload: GraphMappingRequest, user: User = Depends(require_roles("teacher", "admin")), store: Store = Depends(get_store)) -> dict:
    source = store.graph_nodes.get(payload.source_id)
    target = store.graph_nodes.get(payload.target_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail={"code": "node_not_found", "message": "映射节点不存在"})
    _check_node_access(source, user)
    _check_node_access(target, user)
    if source["graph"] != "problem" or target["graph"] != "knowledge":
        raise HTTPException(status_code=422, detail={"code": "invalid_mapping", "message": "只允许问题图谱节点映射到知识图谱节点"})
    if payload.source_id == payload.target_id:
        raise HTTPException(status_code=422, detail={"code": "invalid_mapping", "message": "节点不能映射到自身"})
    source["map_from"] = payload.target_id
    store.audit("graph_mapping_update", user.id, {"source_id": payload.source_id, "target_id": payload.target_id})
    return {"status": "ok", "source_id": payload.source_id, "target_id": payload.target_id}


def _validate_mapping(payload: GraphNodeCreate, store: Store, user: User, *, node_id: str | None = None) -> None:
    if payload.map_from is None:
        return
    if payload.graph != "problem":
        raise HTTPException(status_code=422, detail={"code": "invalid_mapping", "message": "只有问题图谱节点可以设置映射"})
    if payload.map_from == node_id:
        raise HTTPException(status_code=422, detail={"code": "invalid_mapping", "message": "节点不能映射到自身"})
    target = store.graph_nodes.get(payload.map_from)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "node_not_found", "message": "映射节点不存在"})
    _check_node_access(target, user)
    if target["graph"] != "knowledge":
        raise HTTPException(status_code=422, detail={"code": "invalid_mapping", "message": "只允许映射到知识图谱节点"})


def _get_node(node_id: str, store: Store) -> dict:
    node = store.graph_nodes.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail={"code": "node_not_found", "message": "图谱节点不存在"})
    return node


def _check_node_access(node: dict, user: User) -> None:
    if user.role != "admin" and node.get("created_by") != user.id:
        raise HTTPException(status_code=403, detail={"code": "forbidden", "message": "无权操作该图谱节点"})
