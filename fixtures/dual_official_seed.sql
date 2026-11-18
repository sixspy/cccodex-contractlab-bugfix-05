-- 双正式并存夹具：同一主题「订单入站」塞入第二个 正式/启用 版本 v3b。
-- 与种子 v3 必填不同（v3 要 currency，v3b 不要），用于复现入站命中不稳。
-- 用法见 fixtures/repro_dual_official.py。
INSERT INTO contracts(version,subject,mode,track,status,required_fields)
VALUES ('v3b','订单入站','严格','正式','启用','order_id,amount');
