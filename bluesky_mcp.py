import os
import sys

# 强制 Windows 使用二进制模式标准输入输出，避免 \r\n 问题
# Antigravity 的 MCP 客户端对 \r (CR) 非常敏感，会导致 "invalid trailing data" 错误
if sys.platform == 'win32':
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

import re
import json
import hashlib
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any, List
from functools import lru_cache


from mcp.server.fastmcp import FastMCP
from atproto import Client, client_utils, models
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
    - **Write**: Create new posts with optional image attachments (`send_post`), and reply to others (`reply_to_post`).
    - **React**: Like (`like_post`) and Repost (`repost`) content.
    - **Moderate**: Block (`block_user`) and unblock (`unblock_user`) users to permanently cut off unwanted accounts.
    - **Monitor**: Check notifications (`get_notifications`).
    
    Operational Rules:
    1. **Character Limit**: Maximum 300 characters per post. The API will fail if exceeded.
    2. **Threading**: To reply, use `reply_to_post` with the target post's URI. The tool handles the threading references automatically.
    3. **Awareness**: Before posting about a topic, it is recommended to search or check the timeline to understand the context.
    4. **Visual Content**: To maximize engagement with human users, generate SVG graphics/infographics, convert them to PNG/JPEG (e.g. via cairosvg or browser screenshot), and pass the file paths via `send_post`'s `image_paths` parameter. Images dramatically increase visibility and engagement.
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


MENTION_REGEX = re.compile(
    r'(?<![a-zA-Z0-9])@(([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)'
)
URL_REGEX = re.compile(r'https?://[^\s]+')


def _build_rich_text(text: str, client: Client) -> client_utils.TextBuilder:
    """Parse @mentions in text and return a TextBuilder with proper mention facets."""
    builder = client_utils.TextBuilder()
    last_end = 0
    handle_to_did = {}
    url_spans = [(match.start(), match.end()) for match in URL_REGEX.finditer(text)]
    current_url_index = 0

    for match in MENTION_REGEX.finditer(text):
        handle = match.group(1)
        mention_start = match.start()

        while current_url_index < len(url_spans) and mention_start >= url_spans[current_url_index][1]:
            current_url_index += 1

        if current_url_index < len(url_spans):
            url_start, url_end = url_spans[current_url_index]
            if url_start <= mention_start < url_end:
                continue

        if mention_start > last_end:
            builder.text(text[last_end:mention_start])

        if handle in handle_to_did:
            did = handle_to_did[handle]
            if did:
                builder.mention(f"@{handle}", did)
            else:
                builder.text(f"@{handle}")
        else:
            try:
                profile = client.get_profile(actor=handle)
                handle_to_did[handle] = profile.did
                builder.mention(f"@{handle}", profile.did)
            except Exception:
                handle_to_did[handle] = None
                builder.text(f"@{handle}")

        last_end = match.end()

    if last_end < len(text):
        builder.text(text[last_end:])

    return builder


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
    image_paths: Optional[List[str]] = None,
    image_alts: Optional[List[str]] = None,
) -> str:
    """
    发送一条新的 Bluesky 帖子。**回复特定帖子请用 reply_to_post 工具别搞错了**。

    CRITICAL LIMITATION: Bluesky posts are strictly limited to 300 characters (300 graphemes).
    If your text exceeds this, the API will return a 400 InvalidRequest error.
    You MUST condense your message to fit within this limit. Be concise.
    Link URLs count towards the limit.

    可选附加图片（最多 4 张，每张 <= 1MB）。图片能极大提高对人类用户的吸引力。
    推荐流程：用 SVG 画图 → 转 PNG/JPEG → 传入 image_paths 发布。

    Args:
        text: 帖子内容 (Must be <= 300 chars)
        link_url: 可选的链接 URL（将在文本末尾添加链接）
        link_title: 链接标题（仅在提供 link_url 时有效）
        link_description: 链接描述（仅在提供 link_url 时有效）
        image_paths: 可选的本地图片文件绝对路径列表（最多 4 张，支持 jpg/png/gif/webp）
        image_alts: 图片替代文字描述列表（无障碍访问用，建议提供）

    Returns:
        发送成功后的帖子 URI，或者包含长度信息的错误提示
    """
    # 估算长度
    text_len = len(text)
    link_display = link_title or link_url or ""
    link_display_len = len(link_display) if link_url else 0
    separator_len = (1 if link_url and not text.endswith(" ") and not text.endswith("\n") else 0)
    total_len = text_len + separator_len + link_display_len

    try:
        client = get_client()

        # 图片校验与读取
        embed = None
        if image_paths:
            if len(image_paths) > 4:
                return json.dumps({
                    "success": False,
                    "error": "Too many images",
                    "details": f"Maximum is 4 images per post. Provided: {len(image_paths)}"
                }, ensure_ascii=False, indent=2)

            images_data = []
            for img_path in image_paths:
                p = Path(img_path)
                if not p.exists():
                    return json.dumps({
                        "success": False,
                        "error": "Image file not found",
                        "details": f"Path: {img_path}"
                    }, ensure_ascii=False, indent=2)

                if not p.is_file():
                    return json.dumps({
                        "success": False,
                        "error": "Path is not a file",
                        "details": f"Path: {img_path}"
                    }, ensure_ascii=False, indent=2)

                size = p.stat().st_size
                if size > 1_000_000:
                    return json.dumps({
                        "success": False,
                        "error": "Image too large",
                        "details": f"Path: {img_path} ({size:,} bytes, max 1,000,000)"
                    }, ensure_ascii=False, indent=2)

                try:
                    images_data.append(p.read_bytes())
                except Exception as read_err:
                    return json.dumps({
                        "success": False,
                        "error": "Failed to read image file",
                        "details": f"Path: {img_path}. Error: {str(read_err)}"
                    }, ensure_ascii=False, indent=2)

            alts = image_alts or []
            if len(alts) < len(images_data):
                alts = alts + [""] * (len(images_data) - len(alts))

            uploads = [client.upload_blob(img) for img in images_data]
            embed_images = [
                models.AppBskyEmbedImages.Image(alt=alt, image=upload.blob)
                for alt, upload in zip(alts, uploads)
            ]
            embed = models.AppBskyEmbedImages.Main(images=embed_images)

        text_builder = _build_rich_text(text, client)

        if link_url:
            if separator_len:
                text_builder.text(" ")
            text_builder.link(link_display, link_url)

        post = client.send_post(text_builder, embed=embed)

        imgs_note = f" with {len(image_paths)} image(s)" if image_paths else ""
        return json.dumps({
            "success": True,
            "uri": post.uri,
            "cid": post.cid,
            "message": f"Post{imgs_note} sent successfully! ({total_len}/300 chars used)"
        }, ensure_ascii=False, indent=2)

    except Exception as e:
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
        if image_paths:
            instruction += " If it mentions blob size, compress the images to under 1MB each."

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

        text_builder = _build_rich_text(text, client)
        post = client.send_post(text_builder, reply_to=reply_ref)

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
        handle: 用户 handle (例如: misaligned-codex.bsky.social)
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
        handle: 用户 handle (例如: misaligned-codex.bsky.social)

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


# ============================================================================
# 屏蔽相关工具
# ============================================================================

@mcp.tool()
def block_user(handle: str) -> str:
    """
    屏蔽一个用户。被屏蔽的用户无法点赞、回复、提及或关注你，
    其帖子和资料也会从你的视野中隐藏。

    Args:
        handle: 用户 handle (例如: spambot.bsky.social)

    Returns:
        屏蔽结果
    """
    client = get_client()

    try:
        profile = client.get_profile(actor=handle)

        viewer = getattr(profile, 'viewer', None)
        if viewer:
            blocking = getattr(viewer, 'blocking', None)
            if blocking:
                return json.dumps({
                    "success": False,
                    "error": f"Already blocking @{handle}",
                    "existing_block_uri": blocking,
                }, ensure_ascii=False, indent=2)

        record = models.AppBskyGraphBlock.Record(
            created_at=client.get_current_time_iso(),
            subject=profile.did
        )
        result = client.app.bsky.graph.block.create(client.me.did, record)

        return json.dumps({
            "success": True,
            "blocked": handle,
            "did": profile.did,
            "block_uri": result.uri,
            "message": f"Blocked @{handle}. They can no longer interact with you."
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": "Failed to block user",
            "details": str(e),
        }, ensure_ascii=False, indent=2)


@mcp.tool()
def unblock_user(handle: str) -> str:
    """
    取消屏蔽一个用户。

    Args:
        handle: 要取消屏蔽的用户 handle

    Returns:
        取消屏蔽结果
    """
    client = get_client()

    try:
        profile = client.get_profile(actor=handle)

        viewer = getattr(profile, 'viewer', None)
        block_uri = None
        if viewer:
            block_uri = getattr(viewer, 'blocking', None)

        if not block_uri:
            return json.dumps({
                "success": False,
                "error": f"Not currently blocking @{handle}",
            }, ensure_ascii=False, indent=2)

        rkey = block_uri.split("/")[-1]
        client.app.bsky.graph.block.delete(client.me.did, rkey)

        return json.dumps({
            "success": True,
            "unblocked": handle,
            "message": f"Unblocked @{handle}."
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": "Failed to unblock user",
            "details": str(e),
        }, ensure_ascii=False, indent=2)


# ============================================================================
# 搜索工具
# ============================================================================

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
        query: 搜索关键词(关键词之间是** AND 逻辑**，输入的关键词越多，得到的结果越少)
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
