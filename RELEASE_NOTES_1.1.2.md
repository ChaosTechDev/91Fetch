# 91Fetch v1.1.2

这是 Windows 本地桌面版修复更新。

## 本次具体修改

- 修复项目解压到中文路径后，Windows 双击启动脚本可能解析失败的问题
- 新增 `launcher.py`，使用 Python Unicode 路径 API 定位项目目录
- 启动器统一负责创建 `.venv`、安装依赖、生成 `site.json` 和启动网页服务
- 将 `启动.cmd` 改为纯 ASCII、CRLF 格式，只作为兼容性启动入口
- 清理发布压缩包中的 `__pycache__`、测试缓存、下载数据和内部工作目录
- 在中英文 README 中补充 Windows 桌面版与独立 Docker 版的功能区别

## 使用方法

下载 `91Fetch-v1.1.2-Windows.zip`，解压到任意目录（包括中文目录），双击 `启动.cmd`。首次运行会自动安装依赖并打开 `http://127.0.0.1:8765`。

升级时保留旧版本的 `downloads/` 目录。
