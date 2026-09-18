91Fetch v1.1.6
@ChaosTechDev ChaosTechDev released this 今天
 v1.1.6本次具体修改
修复登录时 Cookie 保存顺序错误（session.close() 调整到 save_session_cookies() 之后），确保登录后 Cookie 正确保存
扩大端口探测范围从 20 到 100，提升 Windows 环境下启动成功率
使用方法
下载并解压 91Fetch-v1.1.6-Windows.zip，双击启动.cmd。首次运行会自动创建环境、安装依赖并打开网页，默认地址为 http://127.0.0.1:8765。升级时请保留 downloads/目录，任务状态和本地下载索引都在里面。

有问题请在评论区留言反馈，看到之后会修复。
