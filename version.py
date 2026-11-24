"""应用版本与发布信息的单一来源。"""

APP_VERSION = "v1.1.0"
GITHUB_REPOSITORY = "dabaizuihei/NSU-EventGrabber"
RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
# 发布流程会把版本清单发布到 GitHub Pages，更新检查优先走这个公开地址，
# 避免匿名访问 GitHub API 的速率限制。
UPDATE_MANIFEST_URL = "https://dabaizuihei.github.io/NSU-EventGrabber/version.json"
