module.exports = {
  apps: [
    {
      name: "arb-bot",
      script: "arb_bot.py",
      interpreter: "./venv/bin/python",
      cwd: __dirname,
      autorestart: true,
      restart_delay: 10000,
      kill_timeout: 5000,
      out_file: "./logs/out.log",
      error_file: "./logs/error.log",
      merge_logs: true,
      time: true,
    },
  ],
};
