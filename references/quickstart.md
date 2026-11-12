# CSV 批次调用速查

## 初始化与最终导出

输入是实际CSV文件，UTF-8/BOM，id/product_name表头必需，其余五列可空或缺。每行独立，重复ID不丢行。无需任务ID。不要把所有CSV内容反复填进上下文。

```bash
python3 "<skill>/scripts/batch.py" init --csv "<沙箱内实际CSV>" --workspace "<可写工作区>"
python3 "<skill>/scripts/batch.py" show --batch "<实际batch.json>"
python3 "<skill>/scripts/batch.py" export --batch "<实际batch.json>"
```

init返回batch路径、row_index、各行manifest。无效行保留错误，继续其他行。每次独立沙箱初始化一次，后续复用返回路径。最终只输出batch export返回的JSON，不输出某个产品export替代整个批次。

## 逐产品链

按CSV顺序先逐行完成首来源闭环，再扩展可获得新图的产品。每行的manifest不混用，ID相同也不共享可变记录。

```text
真实搜索 → 目标详情页 → 保存真实DOM/HTML
→ record-source → collect download → check
→ 实际读图 → apply_review → record stop
→ batch export保存本轮批次快照 → 下一产品
```

| 步骤 | 命令及必需输入 |
|---|---|
| 能力/类型 | run_state.py configure --manifest 路径 --input 能力.json；vision_available依据真实能力 |
| 检索留痕 | run_state.py record-search --manifest 路径 --input 搜索.json；query/results/evidence_kind |
| 静态HTML后备 | collect.py fetch-page --manifest 路径 --input URL.json；url |
| 来源提取 | run_state.py record-source --manifest 路径 --input 来源.json；source_page_url/page_name/scope_evidence/html或html_file/selectors/material_type |
| 立即下载 | collect.py download --manifest 路径 --input 下载.json；source_id/image_urls |
| 待检清单 | check.py --manifest 路径 |
| 实际读图 | 平台真实图像工具读取review_batch文件 |
| 提交审核 | apply_review.py --manifest 路径 --input 审核.json；asset_ids/reviews |
| 停止本产品 | run_state.py stop --manifest 路径 --input 停止.json；stop_reason |
| 产品JSON内部诊断 | export_result.py --manifest 路径；不能代替最终批次输出 |

脚本入口为python3 "<skill>/scripts/文件名"，详细JSON示例见contracts.md。输入文件用真实写入工具保存，不将产品名直接拼入Shell命令。禁止运行时阅读源码或安装依赖。

## Skill内置浏览器

```bash
python3 "<skill>/scripts/browser_setup.py" --runtime "<沙箱临时浏览器目录>"
python3 "<skill>/scripts/browser.py" --runtime "<同一运行目录>" --input "<浏览器动作.json>" --output "<当前产品manifest目录>/evidence"
```

```json
{
  "url": "https://example.com/product",
  "actions": [
    {"type": "scroll", "pixels": 600},
    {"type": "wait", "selector": ".screenshots"}
  ],
  "scope_selector": ".screenshots",
  "screenshot": false
}
```

本Skill包含浏览器压缩包和Python轮包，不依赖其他Skill/CLI/MCP。setup仅本地离线安装和解压；不能关闭sandbox或使用snap占位程序。browser每次在临时上下文打开url并执行actions，结束即关闭，无session管理。actions最多20项，支持click/fill/scroll/wait，不接受任意JS。

未提供scope_selector返回页面title/url及无障碍快照；提供后返回目标范围html_file，里面由脚本记录实际currentSrc和背景图。record-source还需要页面真实page_name；若区域HTML没有标题，拼接实际读取的标题证据，不编造名字。输出evidence路径放当前产品目录内，再用相对路径记录。

网络请求经受控公开下载器，不支持登录提交、Cookie透传或WebSocket，部分动态站因此不可用时换源。Chromium需目标系统动态库及sandbox权限；默认不开--no-sandbox，启动失败如实记录。截图只是调试文件，读图仍需实际图像工具。

## 限制与处理

- 不设置采集总时长，默认remaining.seconds=null，取消原20%下载保留门。单网络请求仍有限超时，配额/调用次数/输出长度及平台硬限制仍存在。
- selectors支持tag/#id/.class/[属性存在]及后代，不支持任意CSS，整页禁止。
- page_name/alias需真实文字证据，不用标准名或品牌前缀绕过。
- network.py已补固定User-Agent/Accept/Accept-Language，降低空UA兼容性问题；不保证所有站点可访问，失败换路线并记录，不在节点内改源码。
- 图标无app_icon类型，不冒充品牌图；对象存储尚未接入，沙箱路径不是永久链接。
- 单行出错保留result，processing_status标invalid_input/failed，未执行标not_started，不报该产品无图。
- 浏览器/搜索工具故障是批次共享事实，不每行重新探测相同不可用工具。
