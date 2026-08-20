# NGA BBS 爬虫

爬取 NGA 论坛任意帖子，整理为结构化数据，供 RAG（检索增强生成）使用。支持按作者过滤、增量更新，数据以 JSONL 格式持久化，并可导出为 Markdown。

---

## 环境要求

- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- Python 3.11+

```bash
# 安装 uv（如果尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 快速开始

```bash
# 1. 克隆项目
git clone <your-repo-url>
cd nga_scraper

# 2. 安装依赖
uv sync

# 3. 配置 Cookie（见下方说明）
echo "your_cookie_string_here" > cookies.txt

# 4. 开始爬取（必须指定帖子）
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678"
```

也可以只爬某个用户的发言：

```bash
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678&authorid=12345"
# 或
uv run nga-scraper --thread-id 12345678 --author-id 12345
```

---

## Cookie 配置

NGA 需要登录态才能正常访问帖子。Cookie 按以下优先级加载：

| 优先级 | 方式 | 说明 |
|--------|------|------|
| 1 | `--cookies "..."` | CLI 参数直接传入 |
| 2 | `NGA_COOKIES` 环境变量 | `export NGA_COOKIES="..."` |
| 3 | `cookies.txt` 文件 | 项目根目录，已被 `.gitignore` 忽略 |

**获取 Cookie 的方法：**
1. 在浏览器中登录 [bbs.nga.cn](https://bbs.nga.cn)
2. 打开开发者工具（F12）→ Network 面板
3. 随便点开一个 NGA 请求 → Headers → 复制 `Cookie` 字段的值
4. 粘贴到 `cookies.txt`（一行，无需引号）

> ⚠️ `cookies.txt` 已被 `.gitignore` 忽略，**请勿将 Cookie 提交到 Git**。

---

## 使用方法

必须通过 `--url` 或 `--thread-id` 指定要爬取的帖子。URL 中若带 `authorid`，则只爬该用户；也可用 `--author-id` 覆盖。省略作者则爬整帖。

```bash
# 增量更新（从上次断点继续，推荐日常使用）
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678"

# 全量重爬（清空现有数据，从第1页重新开始）
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --full

# 从指定页开始
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --start-page 50

# 只爬指定页范围
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --pages 1-10
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --pages 42

# 导出所有已爬取内容为 Markdown（不触发爬取）
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --export-md

# 爬取完成后同时导出 Markdown
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --export-md --pages 1-10

# 持续监听新帖子（每3分钟检查，Ctrl+C 退出）
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --watch

# 监听模式 + 不写入磁盘（测试用）
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --watch --dry-run

# 测试解析（不写入磁盘）
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --dry-run --pages 1-1 -v

# 调整请求间隔（默认 1.5 秒）
uv run nga-scraper --url "https://bbs.nga.cn/read.php?tid=12345678" --delay 2.0
```

---

## 数据格式

数据按帖子分开存放：`data/{tid}/{uid}/`；未指定作者时为 `data/{tid}/all/`。

### `posts.jsonl`

每行一条 JSON 对象，格式如下：

```json
{
  "post_id": 854271001,
  "thread_id": 12345678,
  "author_id": 12345,
  "author_name": "示例用户",
  "timestamp": "2026-01-12T09:12:00",
  "subject": "帖子标题（仅楼主帖有，其余为空字符串）",
  "content": "主体文字，已去除引用块和 HTML 标签，纯文本",
  "quoted_posts": [
    {
      "quoted_pid": 854270386,
      "quoted_tid": 12345678,
      "quoted_uid": 60027718,
      "quoted_user": "xiaomiwang1",
      "quoted_time": "2026-01-12T09:07:00",
      "quoted_content": "被引用的原文（纯文本）"
    }
  ],
  "raw_content": "<span id='postcontent2' class='postcontent ubbcode'>...</span>",
  "page": 1,
  "floor": 3
}
```

| 字段 | 说明 |
|------|------|
| `post_id` | 帖子唯一 ID（楼主首帖为 0） |
| `timestamp` | 发帖时间，ISO 8601 格式 |
| `content` | 去除引用后的纯文本正文 |
| `quoted_posts` | 引用的内容列表，含被引用用户、时间、原文 |
| `raw_content` | 原始 HTML/UBB 内容（保留备用） |
| `floor` | 楼层号（全局连续，跨页累计） |

### `metadata.json`

记录爬取状态，用于增量续爬：

```json
{
  "thread_id": 12345678,
  "author_id": 12345,
  "author_name": "示例用户",
  "total_pages": 111,
  "last_scraped_page": 80,
  "total_posts_scraped": 1588,
  "last_updated": "2026-04-22T12:01:47+00:00"
}
```

### `posts.md`

人工可读的 Markdown 文件，引用以 blockquote 格式展示，适合直接喂给 AI 工具阅读。

---

## RAG 集成

JSONL 文件可直接对接主流向量数据库框架：

**LangChain：**
```python
from langchain_community.document_loaders import JSONLoader

loader = JSONLoader(
    file_path="data/12345678/12345/posts.jsonl",
    jq_schema=".content",
    metadata_func=lambda record, _: {
        "post_id": record["post_id"],
        "timestamp": record["timestamp"],
        "floor": record["floor"],
        "subject": record["subject"],
    }
)
docs = loader.load()
```

**LlamaIndex：**
```python
from llama_index.core import SimpleDirectoryReader
docs = SimpleDirectoryReader(input_files=["data/12345678/12345/posts.jsonl"]).load_data()
```

**推荐嵌入策略：**
- 嵌入字段：`subject + "\n" + content`（楼主本人的话）
- 元数据：`post_id`、`timestamp`、`floor`、`thread_id`
- 如需包含上下文：将 `quoted_content` 以 `[引用]` 前缀单独嵌入

---

## 项目结构

```
nga_scraper/
├── pyproject.toml              # 项目配置与依赖声明
├── uv.lock                     # 依赖锁定文件
├── .python-version             # Python 版本（3.11）
├── .gitignore                  # 忽略 cookies.txt、data/、.venv/
├── cookies.txt                 # ⚠️ 本地 Cookie（不提交）
├── src/
│   └── nga_scraper/
│       ├── __init__.py
│       ├── scraper.py          # HTTP 请求：GBK 解码、限速、自动重试
│       ├── parser.py           # HTML 解析：BeautifulSoup + UBB quote 嵌套解析
│       ├── storage.py          # 持久化：JSONL append、metadata 原子写入、Markdown 导出
│       └── main.py             # CLI 入口：argparse，支持增量/全量/页范围模式
└── data/                       # ⚠️ 爬取数据（不提交）
    └── {tid}/{uid 或 all}/
        ├── posts.jsonl
        ├── metadata.json
        └── posts.md
```

---

## 技术说明

| 问题 | 解决方案 |
|------|----------|
| NGA 使用 GBK 编码 | `resp.content.decode("gbk", errors="replace")` 强制解码 |
| 作者名在 JS 中动态加载 | 扫描页面 `commonui.userInfo.setAll()` 解析 uid→昵称 |
| UBB `[quote]` 嵌套 | 深度计数器逐字符扫描，只提取最外层 |
| 增量更新去重 | 启动时加载全部 `post_id` 到 set，append 前检查 |
| 崩溃恢复 | 每页写完后才更新 `metadata.json`，重启从断点续爬 |
| 请求限速 | `time.monotonic()` 精确控制间隔，默认 1.5s |
| 网络抖动 | `urllib3.Retry` 指数退避，最多重试 3 次 |

---

## 许可

本项目仅供个人学习和研究使用，请遵守 NGA 社区规则，合理控制爬取频率。
