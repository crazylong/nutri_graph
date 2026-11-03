#!/bin/bash
# 续期 yangshengleme.fun 的 Let's Encrypt 证书（TLS-ALPN-01）
#
# 为什么不用 acme.sh 自带的 cron：
#   alpn 验证必须独占 443，而 nginx 常驻 443。到期时需要先停 nginx、
#   续期、再拉起。这里做成脚本由 cron 每天跑一次；未到期直接退出，
#   对线上零影响（只有真正续期的那一次有几秒中断）。
#
# 部署位置：/root/ssl/renew-cert.sh
# cron：17 3 * * * /root/ssl/renew-cert.sh

set -u
DOMAIN=yangshengleme.fun
CONF=/root/.acme.sh/${DOMAIN}_ecc/${DOMAIN}.conf
LOG=/var/log/acme-renew.log

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

next=$(grep -m1 '^Le_NextRenewTime=' "$CONF" 2>/dev/null | cut -d"'" -f2)
now=$(date +%s)

if [ -z "${next:-}" ]; then
    log "读取不到 Le_NextRenewTime，跳过（$CONF）"
    exit 0
fi

if [ "$now" -lt "$next" ]; then
    exit 0   # 未到期：什么都不做，nginx 保持运行
fi

log "开始续期 $DOMAIN（计划时间 $(date -d @"$next" '+%F %T')）"

systemctl stop nginx
/root/.acme.sh/acme.sh --renew -d "$DOMAIN" --ecc --alpn --tlsport 443 >> "$LOG" 2>&1
rc=$?

# 无论续期成败，nginx 必须起来
systemctl start nginx
log "续期结束 rc=$rc, nginx=$(systemctl is-active nginx)"

exit $rc
