"""Link fixes applied wherever item text is stored.

The Notion connector returns app.notion.com links. Without /p/ they 404, and with it they open where my notion.so
session isn't signed in. www.notion.so/<page id> opens the page in the Notion app or my existing browser session.
"""

import re

NOTION_LINK = re.compile(r"https?://app\.notion\.com/(?:p/)?(?:[\w-]*-)?([0-9a-f]{32})(?:\?[^\s)\]>\"']*)?")


def fix_notion_links(text: str | None) -> str | None:
    if not text:
        return text
    return NOTION_LINK.sub(lambda m: f"https://www.notion.so/{m.group(1)}", text)
