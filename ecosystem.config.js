// pm2 配置：MontClip Review Inbox（127.0.0.1:8775）。
// 启动：pm2 start ecosystem.config.js && pm2 save
// 登录 Windows 后由计划任务 PM2-Resurrect 执行 pm2 resurrect 自动拉起。
// 与 start-jj-review-inbox.cmd 等价；两者不要同时运行（端口会冲突）。
const path = require("node:path");
const home = require("node:os").homedir();
// Machine-specific locations are resolved locally, never committed usernames.
const localAppData = process.env.LOCALAPPDATA || path.join(home, "AppData", "Local");
const python = process.env.REVIEW_INBOX_PYTHON || path.join(localAppData, "Programs", "Python", "Python312", "python.exe");
const ghConfig = process.env.REVIEW_INBOX_GH_CONFIG_DIR || path.join(home, ".config", "gh-jj-agent-qq");
const logDir = process.env.REVIEW_INBOX_LOG_DIR || path.join(home, ".cache", "jj-review-inbox");
const conhost = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "conhost.exe");
module.exports = {
  apps: [
    {
      name: "jj-review-inbox",
      cwd: __dirname,
      // 用无窗口控制台包一层：否则 pm2 下的 python 没有控制台，
      // 它每次调用 gh/git/codex 都会弹出一个一闪而过的命令行窗口。
      script: conhost,
      args: ["--headless", python,
        "-I", "-u", "-X", "utf8", "server.py", "--config", "config.json"],
      interpreter: "none",
      env: { GH_CONFIG_DIR: ghConfig },
      autorestart: true,
      restart_delay: 10000,
      max_restarts: 20,
      min_uptime: 30000,
      windowsHide: true,
      out_file: path.join(logDir, "server.log"),
      error_file: path.join(logDir, "server.log"),
      merge_logs: true,
      time: true,
    },
    {
      // 每小时一次：清理 14 天前的审阅记录（总量上限 500MB）、日志超 20MB 轮转、
      // 数据库 WAL 收缩、收件箱内存超 1GB 时重启。见 housekeep.py。
      name: "jj-review-inbox-housekeep",
      cwd: __dirname,
      script: conhost,
      args: ["--headless", python,
        "-I", "-u", "housekeep.py"],
      interpreter: "none",
      autorestart: false,
      cron_restart: "23 * * * *",
      windowsHide: true,
      out_file: path.join(logDir, "housekeep.log"),
      error_file: path.join(logDir, "housekeep.log"),
      merge_logs: true,
    },
  ],
};
