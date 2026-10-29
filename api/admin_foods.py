"""后台食物查询。挂在 /admin 上，和其它管理接口一样要管理员令牌。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.deps import require_admin
from kg import food_admin

router = APIRouter(tags=["admin-foods"], dependencies=[Depends(require_admin)])


def _fail(exc: food_admin.FoodAdminError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


@router.get("/admin/kg/foods")
def search_foods(q: str = Query("")):
    try:
        return food_admin.search(q)
    except food_admin.FoodAdminError as exc:
        return _fail(exc)


@router.get("/admin/kg/foods/{cid}")
def food_detail(cid: str):
    try:
        return food_admin.food_detail(cid)
    except food_admin.FoodAdminError as exc:
        return _fail(exc)


@router.get("/admin/kg/nutrients")
def nutrient_rank(q: str = Query(""), page: int = Query(1, ge=1)):
    try:
        return food_admin.nutrient_page(q, page)
    except food_admin.FoodAdminError as exc:
        return _fail(exc)


class RelationIn(BaseModel):
    foods: list[str] = Field(default_factory=list)
    confirm: bool = False


@router.post("/admin/kg/foods/relations")
def food_relations(body: RelationIn):
    try:
        return food_admin.relations(body.foods, confirm=body.confirm)
    except food_admin.FoodAdminError as exc:
        return _fail(exc)


class DeleteIn(BaseModel):
    canonical_ids: list[str] = Field(default_factory=list)
    preview: bool = False


@router.post("/admin/kg/foods/delete")
def delete_foods(body: DeleteIn, admin: dict = Depends(require_admin)):
    try:
        return food_admin.delete_foods(body.canonical_ids, admin["user_id"], preview=body.preview)
    except food_admin.FoodAdminError as exc:
        return _fail(exc)


@router.post("/admin/kg/foods/{cid}/rerecord")
def rerecord_food(
    cid: str,
    body: dict[str, Any] = Body(...),
    admin: dict = Depends(require_admin),
):
    try:
        return food_admin.rerecord(cid, body, admin["user_id"])
    except food_admin.FoodAdminError as exc:
        return _fail(exc)
