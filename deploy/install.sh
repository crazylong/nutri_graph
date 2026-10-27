#!/usr/bin/env bash
# deploy/install.sh —— 把 nutri_graph 装成常驻服务 + nginx 站点
#
# 幂等：重复跑只会覆盖配置并 reload，不会重复创建东西。
# 只改这几处：
#   /etc/systemd/system/nutri-graph.service
#   /etc/nginx/conf.d/00-nutri-graph.conf        （http 上下文：upstream + 限流）
#   /etc/nginx/nutri-graph-proxy.conf            （反代公共参数）
#   /etc/nginx/default.d/nutri-graph-locations.conf（80 端口 server 块的扩展点）
#   /var/www/nutri_graph/                        （前端静态文件）
# 不动：/etc/nginx/nginx.conf、已有的 3000 端口站点、项目 .env
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/code/nutri_graph}"
WEB_ROOT="${WEB_ROOT:-/var/www/nutri_graph}"
PY="${PY:-/opt/envs/nutri/bin/python}"
STAMP="$(date +%Y%m%d%H%M%S)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[ -d "$APP_DIR" ] || { echo "找不到项目目录 $APP_DIR"; exit 1; }
[ -x "$PY" ] || { echo "找不到 python：$PY"; exit 1; }
# 后面的 config 自检要能 import 到项目模块
cd "$APP_DIR"

# ---------- 1. 前置检查 ----------
say "1/5 前置检查"
systemctl is-active --quiet docker || echo "  ⚠️ docker 未运行：PG / Neo4j 会连不上"
[ -f "$APP_DIR/.env" ] || { echo "  缺少 $APP_DIR/.env，先按 .env.example 配好"; exit 1; }
"$PY" - <<'EOF' || exit 1
import sys
sys.path.insert(0, ".")
import config
assert config.PG_DSN, "PG_DSN 为空"
assert config.NEO4J_URI, "NEO4J_URI 为空"
print(f"  .env 可用：PG={config.PG_DSN.split('@')[-1]}  Neo4j={config.NEO4J_URI}")
print(f"  AUTH_REQUIRED={config.AUTH_REQUIRED}  短信={config.SMS_PROVIDER}")
if not config.AUTH_REQUIRED:
    print("  ⚠️ AUTH_REQUIRED=false：/chat 免登录，对外只靠 nginx 限流，注意账单")
EOF

# ---------- 2. systemd ----------
say "2/5 安装 systemd 服务"
install -m 0644 "$APP_DIR/deploy/systemd/nutri-graph.service" /etc/systemd/system/nutri-graph.service
systemctl daemon-reload
systemctl enable --now nutri-graph >/dev/null
sleep 2
systemctl is-active --quiet nutri-graph \
  && echo "  nutri-graph 已启动" \
  || { echo "  启动失败，看：journalctl -u nutri-graph -n 50 --no-pager"; exit 1; }

# ---------- 3. 前端静态文件 ----------
say "3/5 部署前端"
mkdir -p "$WEB_ROOT/assets"
install -m 0644 "$APP_DIR/frontend/index.html" "$WEB_ROOT/index.html"
install -m 0644 "$APP_DIR/frontend/assets/styles.css" "$WEB_ROOT/assets/styles.css"
install -m 0644 "$APP_DIR/frontend/assets/app.js" "$WEB_ROOT/assets/app.js"
chown -R root:root "$WEB_ROOT"
find "$WEB_ROOT" -type d -exec chmod 755 {} \;
find "$WEB_ROOT" -type f -exec chmod 644 {} \;
echo "  $WEB_ROOT : $(find "$WEB_ROOT" -type f | wc -l) 个文件"

# ---------- 4. nginx ----------
say "4/5 配置 nginx"
mkdir -p /etc/nginx/default.d
for f in \
  "$APP_DIR/deploy/nginx/00-nutri-graph.conf:/etc/nginx/conf.d/00-nutri-graph.conf" \
  "$APP_DIR/deploy/nginx/nutri-graph-proxy.conf:/etc/nginx/nutri-graph-proxy.conf" \
  "$APP_DIR/deploy/nginx/nutri-graph-locations.conf:/etc/nginx/default.d/nutri-graph-locations.conf" \
; do
  src="${f%%:*}"; dst="${f##*:}"
  [ -f "$dst" ] && cp -a "$dst" "$dst.bak.$STAMP"
  install -m 0644 "$src" "$dst"
  echo "  写入 $dst"
done

nginx -t
systemctl reload nginx
echo "  nginx 已 reload"

# ---------- 5. 验收 ----------
say "5/5 自检"
echo -n "  服务 /health : "
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8810/health || true
echo -n "  经 nginx /   : "
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/ || true
echo -n "  经 nginx /health : "
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/health || true

cat <<EOF

完成。
  页面        http://<公网IP>/
  接口文档    http://<公网IP>/docs
  服务日志    journalctl -u nutri-graph -f
  nginx 日志  tail -f /var/log/nginx/access.log

回滚：
  systemctl disable --now nutri-graph
  rm -f /etc/nginx/default.d/nutri-graph-locations.conf /etc/nginx/nutri-graph-proxy.conf /etc/nginx/conf.d/00-nutri-graph.conf
  nginx -t && systemctl reload nginx
  # 各文件同目录下留有 .bak.$STAMP
EOF
