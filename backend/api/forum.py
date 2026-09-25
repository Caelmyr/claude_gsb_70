"""讨论区 API（按题目分片存储）。"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth, require_admin
from backend.storage import read_json, locked_update, list_files
from backend.utils import now_iso, gen_id, sanitize_id, sort_list

forum_bp = Blueprint("forum", __name__)

# 可选分类（key -> 展示名）；发帖时未选分类归入默认分组
CATEGORIES = {
    "help": "求助",
    "solution": "题解",
    "chat": "闲聊",
}
DEFAULT_CATEGORY = "default"
DEFAULT_CATEGORY_NAME = "未分类"


def category_name(key):
    return CATEGORIES.get(key, DEFAULT_CATEGORY_NAME)


def _forum_path(target):
    return os.path.join(config.FORUM_DIR, f"{sanitize_id(target)}.json")


def _load_forum(target):
    data = read_json(_forum_path(target))
    return (data or {}).get("threads", []) if data else []


def _sort_threads(threads):
    """置顶帖始终排在最前面（多个置顶帖都在最前，组内按置顶时间倒序），
    其余帖子维持原有按 created_at 的排序。"""
    pinned = [t for t in threads if t.get("pinned")]
    normal = [t for t in threads if not t.get("pinned")]
    pinned.sort(key=lambda t: t.get("pinned_at") or t.get("created_at", ""), reverse=True)
    # 保持列表接口原有的时间排序行为不变
    normal = sort_list(normal, key=lambda t: t.get("created_at", ""), reverse=True)
    return pinned + normal


@forum_bp.get("/forum/categories")
def list_categories():
    return ok({
        "default": DEFAULT_CATEGORY,
        "items": [{"key": k, "name": v} for k, v in CATEGORIES.items()],
    })


@forum_bp.get("/forum")
def list_threads():
    problem_id = request.args.get("problem_id")
    contest_id = request.args.get("contest_id")
    category = request.args.get("category")
    target = problem_id or "_general"
    threads = _load_forum(target)
    if contest_id:
        threads = [t for t in threads if t.get("contest_id") == contest_id]
    if category:
        threads = [t for t in threads
                   if (t.get("category") or DEFAULT_CATEGORY) == category]
    threads = _sort_threads(threads)
    return ok({"total": len(threads), "items": threads})


@forum_bp.post("/forum")
@require_auth
def create_thread():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title:
        return err("标题不能为空", 400)
    # 未选分类（或传入未知分类）的帖子归入默认分组，不阻断发帖流程
    category = data.get("category") or DEFAULT_CATEGORY
    if category not in CATEGORIES:
        category = DEFAULT_CATEGORY
    target = data.get("problem_id") or "_general"
    thread = {
        "id": gen_id("t"),
        "problem_id": data.get("problem_id") or None,
        "contest_id": data.get("contest_id"),
        "category": category,
        "category_name": category_name(category),
        "author": request.user["id"],
        "author_name": request.user.get("nickname") or request.user.get("username", ""),
        "title": title,
        "content": content,
        "created_at": now_iso(),
        "pinned": False,
        "pinned_at": None,
        "replies": [],
    }

    def _upd(d):
        if d is None:
            d = {"target": target, "threads": []}
        d.setdefault("threads", []).append(thread)
        return d

    locked_update(_forum_path(target), _upd, default=None)
    return ok(thread)


@forum_bp.get("/forum/<thread_id>")
def get_thread(thread_id):
    for name in list_files(config.FORUM_DIR):
        data = read_json(os.path.join(config.FORUM_DIR, name + ".json"))
        if not data:
            continue
        for t in data.get("threads", []):
            if t["id"] == thread_id:
                return ok(t)
    return err("帖子不存在", 404)


@forum_bp.post("/forum/<thread_id>/reply")
@require_auth
def reply_thread(thread_id):
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return err("回复内容不能为空", 400)
    reply = {
        "id": gen_id("r"),
        "author": request.user["id"],
        "author_name": request.user.get("nickname") or request.user.get("username", ""),
        "content": content,
        "created_at": now_iso(),
    }
    found = [None]

    for name in list_files(config.FORUM_DIR):
        path = os.path.join(config.FORUM_DIR, name + ".json")

        def _upd(d):
            if not d:
                return d
            for t in d.get("threads", []):
                if t["id"] == thread_id:
                    t.setdefault("replies", []).append(reply)
                    found[0] = t
                    break
            return d

        locked_update(path, _upd, default=None)
        if found[0] is not None:
            break

    if found[0] is None:
        return err("帖子不存在", 404)
    return ok(reply)


def _set_pin(thread_id, pinned):
    """跨分片置顶/取消置顶，返回更新后的帖子或 None（未找到）。"""
    result = [None]
    for name in list_files(config.FORUM_DIR):
        path = os.path.join(config.FORUM_DIR, name + ".json")

        def _upd(d):
            if not d:
                return d
            for t in d.get("threads", []):
                if t["id"] == thread_id:
                    t["pinned"] = pinned
                    t["pinned_at"] = now_iso() if pinned else None
                    result[0] = t
                    break
            return d

        locked_update(path, _upd, default=None)
        if result[0] is not None:
            break
    return result[0]


@forum_bp.post("/forum/<thread_id>/pin")
@require_admin
def pin_thread(thread_id):
    t = _set_pin(thread_id, True)
    if t is None:
        return err("帖子不存在", 404)
    return ok(t)


@forum_bp.post("/forum/<thread_id>/unpin")
@require_admin
def unpin_thread(thread_id):
    t = _set_pin(thread_id, False)
    if t is None:
        return err("帖子不存在", 404)
    return ok(t)


@forum_bp.delete("/forum/<thread_id>")
@require_auth
def delete_thread(thread_id):
    removed = [False]
    for name in list_files(config.FORUM_DIR):
        path = os.path.join(config.FORUM_DIR, name + ".json")

        def _upd(d):
            if not d:
                return d
            before = len(d.get("threads", []))
            d["threads"] = [t for t in d.get("threads", [])
                            if not (t["id"] == thread_id and
                                    (t["author"] == request.user["id"] or
                                     request.user.get("role") == "admin"))]
            if len(d.get("threads", [])) != before:
                removed[0] = True
            return d

        locked_update(path, _upd, default=None)
        if removed[0]:
            break

    if not removed[0]:
        return err("帖子不存在或无权删除", 404)
    return ok()
