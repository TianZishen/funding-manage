# Funding Demo

基金自选、历史净值与盘中估值原型。用户在浏览器输入服务器 IP 即可使用；当前没有账号系统，自选基金保存在各浏览器的 `localStorage`。

## 已实现

- 输入6位代码验证并添加基金。
- 展示基金名称、类型、最新正式净值和可切换区间、支持悬停/触摸查看的历史净值图。
- 获取最近一期公开股票持仓。
- 获取 A 股和港股行情；港股估值同时考虑港币兑人民币变化。
- 展示预测涨跌、预测净值、行情覆盖率、置信度和单只持仓贡献。
- 每只基金可在浏览器本地保存当前持仓份额和成本净值，并按盘中预计净值或盘后正式净值计算总盈利与盈利率。
- 手机、平板、PC 响应式布局。

## 盘中与盘后展示

- 北京时间工作日 09:30-15:00：显示预计涨跌、预计净值和上日正式净值。
- 其他时间：显示最近一期正式涨跌、最新正式净值和上日净值，并跳过盘中估值请求。
- 当前按工作日和时间段判断，尚未接入法定节假日交易日历。

## 估值策略

- ETF：直接使用场内实时涨跌，不额外套用股票仓位。
- ETF 联接：解析最新季度报告的目标 ETF 净值占比，再乘目标 ETF 实时涨跌；失败时停止估值。
- 指数基金：使用最新季报股票净值占比；当前数据源无法稳定映射跟踪指数代码时，使用公开持仓代理并降低置信度。
- 普通股票型：使用最新季报股票净值占比、公开股票持仓与实时行情。
- 偏股混合型、灵活配置混合型：使用最新季报股票净值占比、公开股票持仓与实时行情，置信度最高为中。
- API 返回 strategy、estimate_source、position_report_date、position_source 和 confidence_reason；季报仓位缺失时不使用默认值。

## Windows

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run_web.ps1
```

访问 `http://127.0.0.1:8000`。同一局域网的手机可访问脚本打印出的局域网地址。

## Linux 快速运行

```bash
sudo apt update
sudo apt install -y python3 python3-venv
cd /path/to/funding-demo
bash run_web.sh
```

访问 `http://服务器IP:8000`，并在云服务器安全组或防火墙放行 TCP 8000。

## systemd + Nginx

建议将项目上传到 `/opt/funding-demo`：

```bash
sudo apt install -y python3 python3-venv nginx
sudo useradd --system --home /opt/funding-demo --shell /usr/sbin/nologin funddemo || true
sudo chown -R funddemo:funddemo /opt/funding-demo
sudo -u funddemo python3 -m venv /opt/funding-demo/.venv
sudo -u funddemo /opt/funding-demo/.venv/bin/python -m pip install -r /opt/funding-demo/requirements.txt
sudo cp /opt/funding-demo/deploy/funding-demo.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now funding-demo
sudo cp /opt/funding-demo/deploy/nginx-funding-demo.conf /etc/nginx/sites-available/funding-demo
sudo ln -s /etc/nginx/sites-available/funding-demo /etc/nginx/sites-enabled/funding-demo
sudo nginx -t
sudo systemctl reload nginx
```

验证与排查：

```bash
curl http://127.0.0.1:8000/api/health
sudo journalctl -u funding-demo -f
```

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 注意

- Linux 服务器必须能访问东方财富和新浪财经；部分海外 IP 可能被限流。
- 免费数据源不承诺稳定性，公开发布或商业化前需要重新确认数据授权。
- 盘中估值基于滞后的公开持仓，不是基金公司正式净值，不构成投资建议。
