// pm2 配置：MontClip Review Inbox（127.0.0.1:8775）。
// 启动：pm2 start ecosystem.config.js && pm2 save
// 登录 Windows 后由计划任务 PM2-Resurrect 执行 pm2 resurrect 自动拉起。
// 与 start-jj-review-inbox.cmd 等价；两者不要同时运行（端口会冲突）。
module.exports = {
  apps: [
    {
      name: "jj-review-inbox",
      cwd: __dirname,
      // 用无窗口控制台包一层：否则 pm2 下的 python 没有控制台，
      // 它每次调用 gh/git/codex 都会弹出一个一闪而过的命令行窗口。
      script: "C:\\Windows\\System32\\conhost.exe",
      args: ["--headless", "C:\\Users\\tyb90\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
        "-I", "-u", "-X", "utf8", "server.py", "--config", "config.json"],
      interpreter: "none",
      env: { GH_CONFIG_DIR: "C:\\Users\\tyb90\\.config\\gh-jj-agent-qq" },
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 20,
      min_uptime: 30000,
      windowsHide: true,
      out_file: "C:\\Users\\tyb90\\.cache\\jj-review-inbox\\server.log",
      error_file: "C:\\Users\\tyb90\\.cache\\jj-review-inbox\\server.log",
      merge_logs: true,
      time: true,
    },
    {
      // 每小时一次：清理 14 天前的审阅记录（总量上限 500MB）、日志超 20MB 轮转、
      // 数据库 WAL 收缩、收件箱内存超 1GB 时重启。见 housekeep.py。
      name: "jj-review-inbox-housekeep",
      cwd: __dirname,
      script: "C:\\Windows\\System32\\conhost.exe",
      args: ["--headless", "C:\\Users\\tyb90\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
        "-I", "-u", "housekeep.py"],
      interpreter: "none",
      autorestart: false,
      cron_restart: "23 * * * *",
      windowsHide: true,
      out_file: "C:\\Users\\tyb90\\.cache\\jj-review-inbox\\housekeep.log",
      error_file: "C:\\Users\\tyb90\\.cache\\jj-review-inbox\\housekeep.log",
      merge_logs: true,
    },
  ],
};
