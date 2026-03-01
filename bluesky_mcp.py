import os
import sys

# 强制 Windows 使用二进制模式标准输入输出，避免 \r\n 问题
# Antigravity 的 MCP 客户端对 \r (CR) 非常敏感，会导致 "invalid trailing data" 错误
if sys.platform == 'win32':
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

import json
import hashlib
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any
from functools import lru_cache


from mcp.server.fastmcp import FastMCP
from atproto import Client, client_utils
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 创建 MCP 服务器
mcp = FastMCP(
    name="Bluesky MCP",
    instructions="""A client for the Bluesky social network (AT Protocol).
    
    This toolset allows you to function as an autonomous user on Bluesky.
    
    Capabilities:
    - **Read**: Fetch timelines, user profiles (`get_profile`), and search posts/users (`search`).
    - **Write**: Create new posts (`send_post`) and reply to others (`reply_to_post`).
    - **React**: Like (`like_post`) and Repost (`repost`) content.
    - **Monitor**: Check notifications (`get_notifications`).
    
    Operational Rules:
    1. **Character Limit**: Maximum 300 characters per post. The API will fail if exceeded.
    2. **Threading**: To reply, use `reply_to_post` with the target post's URI. The tool handles the threading references automatically.
    3. **Awareness**: Before posting about a topic, it is recommended to search or check the timeline to understand the context.
    """
)


class BlueskyClient:
    """Bluesky 客户端单例，管理登录状态"""

    _instance: Optional["BlueskyClient"] = None
    _client: Optional[Client] = None
    _logged_in: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_client(self) -> Client:
        """获取已登录的客户端"""
        if self._client is None:
            self._client = Client()

        if not self._logged_in:
            handle = os.getenv("BLUESKY_HANDLE")
            password = os.getenv("BLUESKY_PASSWORD")

            if not handle or not password:
                raise ValueError(
                    "Missing BLUESKY_HANDLE or BLUESKY_PASSWORD environment variables. "
                    "Please set them before using this MCP server."
                )

            self._client.login(handle, password)
            self._logged_in = True

        return self._client

    @property
    def me(self):
        """获取当前登录用户的信息"""
        return self.get_client().me


def get_client() -> Client:
    """获取 Bluesky 客户端"""
    return BlueskyClient().get_client()


def _get_attr(obj: Any, path: str, default: Any = None) -> Any:
    """Helper to safely get nested attributes from atproto objects or dicts"""
    parts = path.split(".")
    current = obj
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)

        if current is None:
            return default
    return current


def format_post(post_data: Any, include_reply_context: bool = False) -> dict:
    """格式化帖子数据，使其更易读"""
    # Handle both dict and object input
    if isinstance(post_data, dict):
        post = post_data.get("post", post_data)
    else:
        post = getattr(post_data, "post", post_data)

    # Helper for attribute access
    def get(obj, attr, default=None):
        if isinstance(obj, dict):
            return obj.get(attr, default)
        return getattr(obj, attr, default)

    author = get(post, "author")
    record = get(post, "record")

    result = {
        "uri": get(post, "uri", ""),
        "cid": get(post, "cid", ""),
        "author": {
            "handle": get(author, "handle", ""),
            "display_name": get(author, "display_name", get(author, "displayName", get(author, "handle", ""))),
            "avatar": get(author, "avatar", ""),
        },
        "text": get(record, "text", ""),
        "created_at": get(record, "created_at", get(record, "createdAt", "")),
        "likes": get(post, "like_count", get(post, "likeCount", 0)),
        "reposts": get(post, "repost_count", get(post, "repostCount", 0)),
        "replies": get(post, "reply_count", get(post, "replyCount", 0)),
        "indexed_at": get(post, "indexed_at", get(post, "indexedAt", "")),
    }

    # 如果有嵌入内容（链接卡片、图片等）
    embed = get(post, "embed")
    if embed:
        embed_type = get(embed, "$type") or getattr(embed, "py_type", "")

        if "external" in str(embed_type) or hasattr(embed, "external"):
            external = get(embed, "external")
            result["embed"] = {
                "type": "link",
                "url": get(external, "uri", ""),
                "title": get(external, "title", ""),
                "description": get(external, "description", ""),
            }
        elif "images" in str(embed_type) or hasattr(embed, "images"):
            images = get(embed, "images", [])
            result["embed"] = {
                "type": "images",
                "images": [
                    {"url": get(img, "fullsize", ""), "alt": get(img, "alt", "")}
                    for img in images
                ]
            }

    # 如果是回复，包含回复上下文
    if include_reply_context:
        reply = get(post_data, "reply")
        if reply:
            parent = get(reply, "parent")
            if parent:
                parent_author = get(parent, "author")
                parent_record = get(parent, "record")
                parent_text = get(parent_record, "text", "")
                result["reply_to"] = {
                    "uri": get(parent, "uri", ""),
                    "author": get(parent_author, "handle", ""),
                    "text": parent_text,
                }

    return result


def format_notification(notif: Any) -> dict:
    """格式化通知数据"""
    def get(obj, attr, default=None):
        if isinstance(obj, dict):
            return obj.get(attr, default)
        return getattr(obj, attr, default)

    author = get(notif, "author")
    record = get(notif, "record")

    return {
        "uri": get(notif, "uri", ""),
        "cid": get(notif, "cid", ""),
        "reason": get(notif, "reason", ""),  # like, repost, follow, mention, reply, quote
        "author": {
            "handle": get(author, "handle", ""),
            "display_name": get(author, "display_name", get(author, "displayName", "")),
        },
        "record_text": get(record, "text", ""),
        "indexed_at": get(notif, "indexed_at", get(notif, "indexedAt", "")),
        "is_read": get(notif, "is_read", get(notif, "isRead", False)),
        # 对于 like/repost，包含被互动的帖子信息
        "subject_uri": get(notif, "reason_subject", get(notif, "reasonSubject", "")),
    }


# ============================================================================
# 发帖相关工具
# ============================================================================

@mcp.tool()
def send_post(
    text: str,
    link_url: Optional[str] = None,
    link_title: Optional[str] = None,
    link_description: Optional[str] = None,
) -> str:
    """
    发送一条新的 Bluesky 帖子。**回复特定帖子请用 reply_to_post 工具别搞错了**。

    CRITICAL LIMITATION: Bluesky posts are strictly limited to 300 characters (300 graphemes).
    If your text exceeds this, the API will return a 400 InvalidRequest error.
    You MUST condense your message to fit within this limit. Be concise.
    Link URLs count towards the limit.

    Args:
        text: 帖子内容 (Must be <= 300 chars)
        link_url: 可选的链接 URL（将在文本末尾添加链接）
        link_title: 链接标题（仅在提供 link_url 时有效）
        link_description: 链接描述（仅在提供 link_url 时有效）

    Returns:
        发送成功后的帖子 URI，或者包含长度信息的错误提示
    """
    client = get_client()

    # 估算长度 (近似值，Bluesky 使用 grapheme 计数，Python len() 是 code points)
    # 我们不在本地拦截，因为可能存在计算差异，让 API 决定是否超限
    text_len = len(text)
    link_display = link_title or link_url or ""
    link_display_len = len(link_display) if link_url else 0
    separator_len = (1 if link_url and not text.endswith(" ") and not text.endswith("\n") else 0)
    total_len = text_len + separator_len + link_display_len

    try:
        if link_url:
            # 使用 TextBuilder 构建带链接的帖子
            text_builder = client_utils.TextBuilder()
            text_builder.text(text)
            if separator_len:
                text_builder.text(" ")
            text_builder.link(link_display, link_url)

            post = client.send_post(text_builder)
        else:
            post = client.send_post(text=text)

        return json.dumps({
            "success": True,
            "uri": post.uri,
            "cid": post.cid,
            "message": f"Post sent successfully! ({total_len}/300 chars used)"
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        # 如果 API 报错，大概率是长度问题，提供详细的长度分解帮助 AI 调试
        breakdown = {
            "text_body": f"{text_len} chars"
        }
        instruction = "If the error mentions length/graphemes, shorten the text body."
        if link_url:
            breakdown["link_display_text"] = f"{link_display_len} chars"
            breakdown["separator"] = f"{separator_len} char"
            instruction = ("If the error mentions length/graphemes, shorten the text body or provide a shorter link_title. "
                           "Note: The link_title is the clickable display text. If no link_title is given, "
                           "the full URL is displayed and counts toward the 300 character limit.")

        breakdown["total_approx"] = f"{total_len} chars"
        breakdown["limit"] = 300
        breakdown["over_by_approx"] = f"{max(0, total_len - 300)} chars"

        return json.dumps({
            "success": False,
            "error": "Failed to send post",
            "details": str(e),
            "length_breakdown": breakdown,
            "instruction": instruction
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def reply_to_post(
    post_uri: str,
    text: str,
) -> str:
    """
    回复一条帖子。

    CRITICAL LIMITATION: Text must be <= 300 characters.

    Args:
        post_uri: 要回复的帖子 URI (格式: at://did:plc:xxx/app.bsky.feed.post/xxx)
        text: 回复内容

    Returns:
        回复帖子的 URI，或者包含长度信息的错误提示
    """
    client = get_client()
    text_len = len(text)

    try:
        # 获取原帖信息以构建回复引用
        parent_post = client.get_post_thread(post_uri)
        parent = parent_post.thread.post

        # 构建回复引用
        reply_ref = {
            "root": {
                "uri": parent.uri,
                "cid": parent.cid,
            },
            "parent": {
                "uri": parent.uri,
                "cid": parent.cid,
            }
        }

        # 如果原帖本身是回复，需要追溯到根帖子
        if hasattr(parent.record, "reply") and parent.record.reply:
            reply_ref["root"] = {
                "uri": parent.record.reply.root.uri,
                "cid": parent.record.reply.root.cid,
            }

        post = client.send_post(text=text, reply_to=reply_ref)

        return json.dumps({
            "success": True,
            "uri": post.uri,
            "cid": post.cid,
            "replied_to": parent.author.handle,
            "message": f"Replied successfully to @{parent.author.handle}! ({text_len}/300 chars used)"
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": "Failed to reply to post",
            "details": str(e),
            "length_breakdown": {
                "text_body": f"{text_len} chars",
                "total_approx": f"{text_len} chars",
                "limit": 300,
                "over_by_approx": f"{max(0, text_len - 300)} chars",
            },
            "instruction": "If the error mentions length/graphemes, shorten the reply text to under 300 characters."
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def delete_post(post_uri: str) -> str:
    """
    删除一条帖子。

    Args:
        post_uri: 要删除的帖子 URI

    Returns:
        删除结果
    """
    client = get_client()

    # 使用 unsend 来删除帖子 (delete_post 需要 rkey，unsend 更方便)
    success = client.delete_post(post_uri)

    return json.dumps({
        "success": True,
        "deleted_uri": post_uri,
        "message": "Post deleted successfully!"
    }, ensure_ascii=False, indent=2)


# ============================================================================
# 浏览相关工具
# ============================================================================

@mcp.tool()
def get_timeline(limit: int = 20, cursor: Optional[str] = None) -> str:
    """
    获取首页时间线（关注的人的帖子）。

    Args:
        limit: 获取帖子数量，最大 100
        cursor: 分页游标，用于获取下一页

    Returns:
        时间线帖子列表
    """
    client = get_client()

    timeline = client.get_timeline(limit=min(limit, 100), cursor=cursor)

    posts = [format_post(item, include_reply_context=True) for item in timeline.feed]

    return json.dumps({
        "posts": posts,
        "cursor": timeline.cursor,
        "count": len(posts),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_author_feed(
    handle: str,
    limit: int = 20,
    cursor: Optional[str] = None,
) -> str:
    """
    获取某个用户的帖子列表。

    Args:
        handle: 用户 handle (例如: nocturne.bsky.social)
        limit: 获取帖子数量，最大 100
        cursor: 分页游标

    Returns:
        用户帖子列表
    """
    client = get_client()

    feed = client.get_author_feed(actor=handle, limit=min(limit, 100), cursor=cursor)

    posts = [format_post(item, include_reply_context=True) for item in feed.feed]

    return json.dumps({
        "author": handle,
        "posts": posts,
        "cursor": feed.cursor,
        "count": len(posts),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_post_thread(post_uri: str, depth: int = 6) -> str:
    """
    获取帖子及其回复线程。

    Args:
        post_uri: 帖子 URI
        depth: 获取回复深度，最大 6

    Returns:
        帖子线程（包括父帖和回复）
    """
    client = get_client()

    thread = client.get_post_thread(uri=post_uri, depth=min(depth, 6))

    def format_thread_post(thread_item):
        """递归格式化线程中的帖子"""
        if not thread_item or not hasattr(thread_item, "post"):
            return None

        result = format_post({"post": thread_item.post})

        # 处理回复
        if hasattr(thread_item, "replies") and thread_item.replies:
            result["replies"] = [
                format_thread_post(reply)
                for reply in thread_item.replies
                if reply and hasattr(reply, "post")
            ]
            result["replies"] = [r for r in result["replies"] if r]

        return result

    # 格式化主帖
    main_post = format_thread_post(thread.thread)

    # 格式化父帖（如果有）
    parent_chain = []
    if hasattr(thread.thread, "parent") and thread.thread.parent:
        parent = thread.thread.parent
        while parent and hasattr(parent, "post"):
            parent_chain.insert(0, format_post({"post": parent.post}))
            parent = getattr(parent, "parent", None)

    return json.dumps({
        "parent_chain": parent_chain,
        "post": main_post,
    }, ensure_ascii=False, indent=2)


# ============================================================================
# 互动相关工具
# ============================================================================

@mcp.tool()
def like_post(post_uri: str) -> str:
    """
    点赞一条帖子。

    Args:
        post_uri: 帖子 URI

    Returns:
        点赞结果
    """
    client = get_client()

    # 获取帖子的 cid
    thread = client.get_post_thread(uri=post_uri)
    post = thread.thread.post

    like = client.like(uri=post.uri, cid=post.cid)

    return json.dumps({
        "success": True,
        "liked_post": post_uri,
        "like_uri": like.uri,
        "author": post.author.handle,
        "message": f"Liked @{post.author.handle}'s post!"
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def unlike_post(post_uri: str) -> str:
    """
    取消点赞一条帖子。

    Args:
        post_uri: 帖子 URI

    Returns:
        取消点赞结果
    """
    client = get_client()

    success = client.unlike(post_uri)

    return json.dumps({
        "success": True,
        "unliked_post": post_uri,
        "message": "Unliked successfully!"
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def repost(post_uri: str) -> str:
    """
    转发一条帖子。

    Args:
        post_uri: 帖子 URI

    Returns:
        转发结果
    """
    client = get_client()

    # 获取帖子的 cid
    thread = client.get_post_thread(uri=post_uri)
    post = thread.thread.post

    repost_ref = client.repost(uri=post.uri, cid=post.cid)

    return json.dumps({
        "success": True,
        "reposted_post": post_uri,
        "repost_uri": repost_ref.uri,
        "author": post.author.handle,
        "message": f"Reposted @{post.author.handle}'s post!"
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def unrepost(post_uri: str) -> str:
    """
    取消转发一条帖子。

    Args:
        post_uri: 帖子 URI

    Returns:
        取消转发结果
    """
    client = get_client()

    success = client.unrepost(post_uri)

    return json.dumps({
        "success": True,
        "unreposted_post": post_uri,
        "message": "Unreposted successfully!"
    }, ensure_ascii=False, indent=2)


# ============================================================================
# 通知相关工具
# ============================================================================

@mcp.tool()
def get_notifications() -> str:
    """
    获取通知列表。
    
    自动处理逻辑：
    1. 检查未读数量。
    2. 如果有未读：自动循环获取所有未读通知（有安全上限），展示它们，并将所有未读标记为已读。
    3. 如果无未读：获取最近 10 条历史通知以供参考。

    Returns:
        JSON 字符串，包含通知列表和状态信息。
    """
    client = get_client()

    # 1. 获取未读计数
    unread_data = client.app.bsky.notification.get_unread_count({})
    total_unread = unread_data.count
    
    notifications = []
    status_msg = ""
    
    if total_unread > 0:
        # 有未读消息：循环获取直到拿到所有未读
        # 设置一个安全上限 (例如 200) 防止上下文溢出
        SAFETY_LIMIT = 200
        cursor = None
        
        # 循环拉取
        while len(notifications) < total_unread and len(notifications) < SAFETY_LIMIT:
            remaining = total_unread - len(notifications)
            batch_limit = min(50, remaining)  # 不再 +10，精确拉取
            
            resp = client.app.bsky.notification.list_notifications(
                {"limit": batch_limit, "cursor": cursor}
            )
            
            if not resp.notifications:
                break
                
            notifications.extend(resp.notifications)
            cursor = resp.cursor
            
            if not cursor:
                break
        
        # 只保留未读的（以防 API 返回了一些混杂的已读消息）
        # 注意：atproto SDK 可能使用 is_read 或 isRead，用 getattr 安全访问
        def is_unread(n):
            return not (getattr(n, 'is_read', None) or getattr(n, 'isRead', False))
        
        unread_notifications = [n for n in notifications if is_unread(n)]
        
        # 如果过滤后列表为空（比如 API 计数延迟），退化为使用所有获取到的
        target_list = unread_notifications if unread_notifications else notifications
        
        # 获取完毕后，再标记为已读
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        client.app.bsky.notification.update_seen({"seenAt": now})
        
        status_msg = f"Fetched {len(target_list)} unread notifications and marked all as read."
        notifications = target_list
        
    else:
        # 无未读消息：获取最近 10 条作为上下文
        resp = client.app.bsky.notification.list_notifications({"limit": 10})
        notifications = resp.notifications
        status_msg = "No new notifications. Showing recent history."

    # 格式化
    formatted_notifs = [format_notification(n) for n in notifications]

    return json.dumps({
        "notifications": formatted_notifs,
        "count": len(formatted_notifs),
        "total_unread_was": total_unread,
        "status": status_msg
    }, ensure_ascii=False, indent=2)


# ============================================================================
# 社交关系相关工具
# ============================================================================

@mcp.tool()
def get_profile(handle: str) -> str:
    """
    获取用户资料。

    Args:
        handle: 用户 handle (例如: nocturne.bsky.social)

    Returns:
        用户资料信息
    """
    client = get_client()

    profile = client.get_profile(actor=handle)

    return json.dumps({
        "did": profile.did,
        "handle": profile.handle,
        "display_name": profile.display_name or profile.handle,
        "description": profile.description or "",
        "avatar": profile.avatar or "",
        "banner": profile.banner or "",
        "followers_count": profile.followers_count or 0,
        "follows_count": profile.follows_count or 0,
        "posts_count": profile.posts_count or 0,
        "indexed_at": profile.indexed_at or "",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def follow_user(handle: str) -> str:
    """
    关注一个用户。

    Args:
        handle: 要关注的用户 handle

    Returns:
        关注结果
    """
    client = get_client()

    # 先获取用户的 DID
    profile = client.get_profile(actor=handle)

    follow = client.follow(profile.did)

    return json.dumps({
        "success": True,
        "followed": handle,
        "follow_uri": follow.uri,
        "message": f"Now following @{handle}!"
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def unfollow_user(handle: str) -> str:
    """
    取消关注一个用户。

    Args:
        handle: 要取消关注的用户 handle

    Returns:
        取消关注结果
    """
    client = get_client()

    # 先获取用户的 DID
    profile = client.get_profile(actor=handle)

    success = client.unfollow(profile.did)

    return json.dumps({
        "success": True,
        "unfollowed": handle,
        "message": f"Unfollowed @{handle}!"
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def search(
    query: str,
    type: str = "posts",
    limit: int = 25,
    cursor: Optional[str] = None,
) -> str:
    """
    搜索帖子或用户。

    Args:
        query: 搜索关键词(and 逻辑，输入的关键词越多，得到的结果越少)
        type: 搜索类型，"posts" 或 "users"，默认 "posts"
        limit: 返回数量，最大 100
        cursor: 分页游标

    Returns:
        搜索结果
    """
    client = get_client()

    if type == "users":
        results = client.app.bsky.actor.search_actors({
            "q": query,
            "limit": min(limit, 100),
            "cursor": cursor,
        })

        users = [
            {
                "did": u.did,
                "handle": u.handle,
                "display_name": u.display_name or u.handle,
                "description": (u.description or "")[:200],
                "avatar": u.avatar or "",
            }
            for u in results.actors
        ]

        return json.dumps({
            "query": query,
            "type": "users",
            "users": users,
            "cursor": results.cursor if hasattr(results, "cursor") else None,
            "count": len(users),
        }, ensure_ascii=False, indent=2)

    else:  # posts
        results = client.app.bsky.feed.search_posts({
            "q": query,
            "limit": min(limit, 100),
            "cursor": cursor,
        })

        posts = [format_post({"post": p}) for p in results.posts]

        return json.dumps({
            "query": query,
            "type": "posts",
            "posts": posts,
            "cursor": results.cursor if hasattr(results, "cursor") else None,
            "count": len(posts),
        }, ensure_ascii=False, indent=2)


# ============================================================================
# MCP 资源 (可选，用于暴露一些静态信息)
# ============================================================================

@mcp.resource("bluesky://profile")
def get_current_profile_resource() -> str:
    """
    当前登录用户的资料（作为 MCP 资源）。
    """
    client = get_client()
    return get_profile(client.me.handle)


@mcp.resource("bluesky://notifications/unread")
def get_unread_count_resource() -> str:
    """
    未读通知数量（作为 MCP 资源）。
    """
    client = get_client()
    unread = client.app.bsky.notification.get_unread_count({})
    return json.dumps({"unread_count": unread.count}, ensure_ascii=False)


# ============================================================================
# 图片下载工具
# ============================================================================

# 项目根目录
_PROJECT_ROOT = Path(__file__).parent
_DOWNLOAD_DIR = _PROJECT_ROOT / "downloaded_images"


@mcp.tool()
def download_image(url: str) -> str:
    """
    下载图片到本地。你在后续可使用read工具自己查看图片。
    
    Args:
        url: 图片URL
    
    Returns:
        下载后的本地绝对路径
    """
    # 按日期创建子目录
    today = datetime.now().strftime("%Y-%m-%d")
    save_dir = _DOWNLOAD_DIR / today
    save_dir.mkdir(parents=True, exist_ok=True)
    
    # 从 URL 推断扩展名
    if "@" in url:
        ext = url.split("@")[-1].lower()
    else:
        ext = url.split(".")[-1].lower()
        
    if ext not in ("jpeg", "jpg", "png", "gif", "webp"):
        ext = "jpg"
    
    # 使用 URL 的 hash 作为文件名
    safe_filename = hashlib.md5(url.encode()).hexdigest()[:12]
    file_path = save_dir / f"{safe_filename}.{ext}"
    
    # 如果已存在则跳过下载
    if file_path.exists():
        return json.dumps({
            "success": True,
            "local_path": str(file_path.resolve()),
            "already_existed": True
        }, ensure_ascii=False, indent=2)
    
    # 下载图片
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, follow_redirects=True)
            response.raise_for_status()
            
            file_path.write_bytes(response.content)
            
            return json.dumps({
                "success": True,
                "local_path": str(file_path.resolve())
            }, ensure_ascii=False, indent=2)
    
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "url": url
        }, ensure_ascii=False, indent=2)


# ============================================================================
# 入口点
# ============================================================================

if __name__ == "__main__":
    # 使用 stdio 传输运行 MCP 服务器
    mcp.run()
