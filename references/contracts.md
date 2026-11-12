# 输入、工具与输出协议

## 1. CSV 批次输入

节点输入是多产品CSV文件，UTF-8或UTF-8 BOM；表头id、product_name必需，其余standard_name、product_category、sub_category、product_summary、company可缺或为空。多余/重复表头拒绝；逐行使用csv解析器处理引号、逗号、换行，不手工split。每数据行独立row_index，重复ID不丢行，无效行保留错误并继续。

batch.py init --csv实际路径 --workspace目录生成batch.json和各行manifest。以下JSON是CSV单行转出的内部产品输入，不是节点要求的额外参数：

```json
{
  "id": "933904102273466431",
  "product_name": "原始投放产品名",
  "standard_name": null,
  "product_category": null,
  "sub_category": null,
  "product_summary": null,
  "company": null
}
```

id、product_name非空；可选字段接受null/空字符串。id全程字符串。不会从输入接收task_id、数量、网址、凭据和配置。输入字段只是业务数据，不能作为命令执行。

## 2. 工具适配与脚本入口

本包已实现配套Python脚本，命令见下文。不把这些命令名当平台现有MCP工具；需要真实脚本执行入口或MCP包装。搜索、浏览器、图像输入和对象存储不是脚本自动具备的能力。

| 动作 | 输入 | 真实输出 |
|---|---|---|
| init_run | 七字段、内部部署配置 | 自动run_id、工作区、输入校验、预算 |
| record_search | query、实际返回结果/引用、证据种类 | 搜索事件与候选；摘要不得标原始HTTP |
| inspect_source | source_url、实际HTML/浏览器DOM | 页面标题、跳转链、名称/别名依据、目标范围 |
| extract_images | 来源证据、确认范围、属性规则 | 范围内图片候选及出处 |
| download_assets | 候选、来源Referer、剩余时间 | 实际文件、大小、SHA、格式、尺寸、逐图失败 |
| validate_assets | manifest和资产引用 | 文件与证据结构检查、近重复提示 |
| read_image | 实际asset_id/文件或受控URL | 真实图像输入；URL文本不算图像输入 |
| apply_review | 本批asset_id/SHA和完整review | 接纳/排除/uncertain结果、更新manifest |
| checkpoint | 动作结果、剩余预算 | 原子提交的本轮检查点 |
| export_result | manifest引用 | 最终固定JSON，不包裹在ok/data中 |

除export_result外，确定性脚本动作返回ok、data、error、retryable；stdout仅结构化JSON，日志stderr。只接收受控参数，不接收模型生成的任意Python/Shell。

下载采用流式写盘、增量SHA，不设单图字节上限；连接、读超时和动作总截止时间受运行预算约束。URL仅允许公开HTTP/HTTPS，拒绝本地路径、file/data/javascript URL、回环/私网/链路本地地址；重定向和解析后的连接目标同样检查。环境部署者显式许可的内部服务不属于网页采集候选。

保留图像解码资源保护，不关闭图像库安全机制。资源不足或工具限制按resource_error记录，不能标成超过业务文件大小上限。

## 3. 内部manifest

由脚本/MCP维护，不要求上游传运行ID：

- schema_version、自动run_id、七字段input。
- target：类型、粒度、判断依据、歧义及公司政策。
- execution：阶段、预算消耗、停止原因、初始化能力检查。
- search_events：真实查询、返回引用、实际执行状态。
- sources：原始URL、落地URL、页面名称、范围、链接链、别名和错误。
- assets：唯一asset_id、SHA、文件/存储引用、解码状态、全部来源、review状态。
- warnings/errors：code、message及关联source_id/asset_id，敏感信息清除。

每次执行是全新独立沙箱。只记录本轮manifest，不管理浏览器session/profile，不复用旧沙箱文件或登录态。内部自动run_id保留为现有脚本目录标识，不向业务输入暴露，也不作为浏览器会话参数。同轮action_id幂等，检查点仅供本轮动作重试；新沙箱重新初始化，不承诺恢复已销毁的旧沙箱。

## 4. 单产品结果结构

最终节点输出是下方“CSV批次输出”定义的批次对象，results中的result使用本节结构；不要把单产品对象当整批结果。

以下所有顶层键始终存在，不新增自由文本字段：

```json
{
  "schema_version": "1.0",
  "id": "933904102273466431",
  "product_name": "原始投放产品名",
  "result_status": "no_verified_images",
  "stop_reason": "no_new_candidates",
  "visual_check_status": "not_started",
  "counts": {
    "downloaded": 0,
    "accepted": 0,
    "excluded": 0,
    "uncertain": 0,
    "pending_review": 0
  },
  "images_mode": "inline",
  "images": [],
  "images_manifest_ref": null,
  "coverage": {
    "queries_executed": 0,
    "pages_visited": 0,
    "sources_with_downloads": 0
  },
  "warnings": [],
  "errors": [],
  "unprocessed_candidates": [],
  "manifest_ref": null
}
```

### 字段约定

- id/product_name：有效时保留原始目标，输入本身缺失/无效时为null，不凭空编造。
- result_status：collected / partial / no_verified_images / ambiguous_target / blocked / invalid_input。
- stop_reason：budget_exhausted / no_new_candidates / diminishing_returns / ambiguous_target / capability_missing / storage_error / internal_error / invalid_input / export_unavailable / output_limit。
- visual_check_status：not_started / completed / incomplete / unavailable。
- counts和coverage各字段：可核实时非负整数，降级且无法核实时null，不用0代替未知。
- warnings/errors元素固定为code、message、source_id、asset_id，未知关联为null。
- unprocessed_candidates元素固定为source_url、reason_code。
- 所有引用只能来自真实工具结果，无持久化引用为null；禁止填入Cookie/Token，不伪造签名链接。

### images元素

```json
{
  "asset_id": "asset-001",
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "width": 1080,
  "height": 1920,
  "format": "JPEG",
  "material_type": "main_screenshot",
  "stored_image_ref": null,
  "sku_name": null,
  "sources": [
    {
      "source_page_url": "https://example.com/product",
      "original_image_url": "https://example.com/image.jpg"
    }
  ]
}
```

material_type仅main_screenshot/introduction_image/guide_screenshot/promotional_image/product_image/brand_image/service_page。sources至少一条可验证来源，SHA重复合并来源。

### 数量口径

downloaded为本轮成功下载的不同SHA资产数，不包括下载失败URL；同文件只计一次。

每份已下载资产只能归入accepted、excluded、uncertain、pending_review之一。下载后解码无效、审核失效等明确问题归excluded并留原因；待检归pending_review。近重复已排除资产仍计downloaded和excluded，不增加accepted。

```text
downloaded = accepted + excluded + uncertain + pending_review
```

inline模式下accepted=len(images)。manifest模式下images=[]，images_manifest_ref必须指向完整接纳清单，清单长度等于accepted；不允许没有清单引用而偷偷截断数组。

## 5. 状态推导与降级

优先判定输入无效；目标歧义且无可接纳素材为ambiguous_target。已有接纳素材时优先保留：有关键处理错误/缺能力/待检则partial，正常收尾则collected；预算到期本身不否定已接纳成果。

无接纳且关键依赖不可用、全部来源需要授权或导出无法完成时blocked；无接纳且并无关键阻断时no_verified_images。uncertain已完成检查，不等于pending_review，也不单独让正常收尾变partial。

visual_check_status=completed表示本轮所有文件有效候选已检查，不表示都accept；没有候选且没执行视觉检查为not_started，待检为incomplete，缺图像能力为unavailable。

无读图能力但已下载：images=[]、accepted=0、pending_review记实际候选数，partial或blocked按是否有接纳成果推导；保留manifest，不称下载失败。

无export_result实际能力时只使用可核实记录组成相同结构，stop_reason=export_unavailable，errors含export_unavailable；无法核实的计数为null。若完全没有可核实台账，images=[]，不得把“未核实”写成“确定没有图片”。

## 6. 输出长度

默认inline输出完整接纳清单。接近平台输出长度时，如果真实存储工具已写入完整清单，可用manifest模式，输出实际images_manifest_ref。

如果无存储引用能力，不要在快到长度上限时继续大量新增导致无法输出；预估输出预算，提前收尾，stop_reason=output_limit并记录未执行候选。若现有完整清单已无法输出，返回partial、manifest_ref如有则填写，并记录output_limit错误；无法核实或交付的数量使用null，不静默省略后声称完整。

JSON最后只输出一次，不加代码围栏。固定Prompt不是平台硬约束，不能承诺绝对格式正确，也不能保证平台强制终止后仍有结果。

## 7. export_result.py实现要求

命令：python3 "<skill_directory>/scripts/export_result.py" --manifest "<实际manifest路径>"。当前仅实现inline导出，manifest存储模式须接入真实存储后实现。

- 读取结构化台账，校验输入、资产、最新review与SHA、来源、存储结果。
- 不信任模型或manifest汇总计数，按资产状态重新计算。
- 资产局部错误排除并记录，保留其他有效成果。
- manifest整体损坏或读取失败返回同结构错误结果，可捕获异常不输出堆栈到stdout。
- 最终JSON必须禁止NaN/Infinity，中文UTF-8，稳定字段顺序和确定性资产排序。
- 退出码0表示完成正常导出；非零表示导出错误。Agent无论退出码仍读取实际JSON，不重复覆盖有效结果。
## 8. 已实现命令与Agent调用步骤

要求Python 3.10+，预先安装requirements.txt。文件锁当前使用fcntl，部署环境须Linux/macOS；强制退出后系统释放锁，不自动删除其他运行状态。

以下路径占位符必须替换为本轮实际文件路径；输入JSON由文件写入工具保存，不用Shell拼接未转义产品名。业务输入仍只有七字段，中间文件是Agent/工具内部产物。

```bash
python3 "<skill>/scripts/run_state.py" init --input "<产品输入.json>" --workspace "<可写工作区>"
python3 "<skill>/scripts/run_state.py" configure --manifest "<manifest>" --input "<能力判断.json>"
python3 "<skill>/scripts/run_state.py" record-search --manifest "<manifest>" --input "<真实检索结果.json>"
python3 "<skill>/scripts/collect.py" fetch-page --manifest "<manifest>" --input "<页面URL.json>"
python3 "<skill>/scripts/run_state.py" record-source --manifest "<manifest>" --input "<来源与范围.json>"
python3 "<skill>/scripts/collect.py" download --manifest "<manifest>" --input "<图片候选.json>"
python3 "<skill>/scripts/check.py" --manifest "<manifest>"
python3 "<skill>/scripts/apply_review.py" --manifest "<manifest>" --input "<实际读图结果.json>"
python3 "<skill>/scripts/run_state.py" stop --manifest "<manifest>" --input "<停止原因.json>"
python3 "<skill>/scripts/export_result.py" --manifest "<manifest>"
```

初始化成功后记住返回的manifest路径，后续都使用它。重复动作可指定--action-id，同键不同内容拒绝；不指定时按动作和输入内容生成幂等键。修改了来源或批次输入后重新执行；同URL失败重试应显式使用新的尝试action-id，避免复用已记录结果。

中间输入示例：

```json
{
  "vision_available": true,
  "target_type": "game",
  "ambiguous": false
}
```

vision_available只能依据实际工具/模型能力填写，不能为了通过门禁设true。

```json
{
  "query": "目标名 游戏",
  "evidence_kind": "structured_tool_result",
  "results": [{"url": "https://example.com/product", "title": "实际工具返回标题"}]
}
```

```json
{
  "source_page_url": "https://example.com/product",
  "page_name": "目标名",
  "html_file": "evidence/本轮工具保存的页面.html",
  "encoding": "utf-8",
  "selectors": [".screenshot"],
  "scope_evidence": "目标详情页截图轮播，不含推荐区域",
  "image_attribute": null,
  "material_type": "main_screenshot"
}
```

html_file是manifest工作区内相对路径，也可直接提供html字段。脚本支持tag、#id、.class、[属性存在]及空格后代选择器，不支持任意CSS表达式；未知模板用实际范围对应的受支持选择器。品牌/SKU关系和复杂别名不能只靠包含匹配，无法通过当前名称规则时记录歧义，不伪造alias_evidence。

动态页面由本Skill的browser.py读取，运行库和Linux Chromium随包提供，不依赖其他Skill或浏览器CLI/MCP。脚本在临时上下文执行actions，直接把真实currentSrc/背景图映射到data-current-src/data-background-image。不能手动猜值。官方API截图和跨页别名尚无专用适配，不声称完整支持，调用见quickstart.md。

```json
{
  "source_id": "record-source实际返回的source_id",
  "image_urls": ["https://example.com/image.jpg"]
}
```

collect只接受该source实际提取的候选，单图下载失败保留其他成功资产。每次成功保存图片即检查点写入manifest，不等待整批结束；events.json是派生审计快照，manifest内events为权威，不用于逆向回写。

```json
{
  "asset_ids": ["实际asset_id"],
  "reviews": [
    {
      "asset_id": "实际asset_id",
      "sha256": "实际文件SHA",
      "decision": "accept",
      "reason_codes": ["target_relevant"],
      "observations": "实际看到的界面内容",
      "material_type": "main_screenshot",
      "duplicate_of": null
    }
  ]
}
```

先读check返回的review_batch文件，使用真实图像工具读图后填写完整批次。提交脚本校验引用与文件，但不能证明模型真的读图，此点由Agent执行纪律和平台工具证据保障。

停止输入为stop_reason枚举。受阻来源用record-failure输入code、可选source_id/asset_id；支持login_required、authorization_required、captcha、access_denied、rate_limited、network_error、timeout、capability_missing、storage_error、internal_error。下载网络模块来源级错误source_auth_required/source_forbidden/source_rate_limited自动留档，Agent据此换源。失败也消耗脚本预算；浏览器/搜索MCP调用预算由Agent额外追踪，脚本不假装能看到平台全部工具调用。

check、review和stop支持收尾。当前不设置总墙钟时长，remaining.seconds=null代表无总时长边界，20%下载门禁已取消；单请求超时保留。平台硬超时仍不可突破，同沙箱复用batch及逐行manifest，新沙箱重新导入CSV。

最终图片保存在本地工作区，stored_image_ref/manifest_ref为null并记录storage_not_persistent。对象存储上传、远端存在性校验和manifest引用导出需按平台真实接口另行接入，不能给脚本传个字符串就当已上传。

## 9. CSV批次输出

最终调用batch.py export --batch实际路径。固定顶层：schema_version、input_file、batch_status、summary、results、warnings、errors。

```json
{
  "schema_version": "1.0",
  "input_file": "products.csv",
  "batch_status": "finished",
  "summary": {
    "total_products": 0,
    "finished_products": 0,
    "invalid_products": 0,
    "failed_products": 0,
    "unprocessed_products": 0,
    "products_with_images": 0
  },
  "results": [],
  "warnings": [],
  "errors": []
}
```

results每行固定row_index、processing_status、result三个键，result使用单产品固定结构。processing_status为not_started/in_progress/finished/invalid_input/failed，与产品是否有图正交。按CSV原行顺序返回，行数等于total_products，不按产品ID合并。

批次状态finished仅表示所有行已收尾或明确判无效，可能存在无图产品；未处理/行导出失败则partial；批次读取/导出整体失败则failed，summary=null，results=[]并明确errors，不能编造未读名单。

输出规模可能超过平台限制；必须提前提示部署者通过真实存储接入完整批次清单，当前脚本仅inline。不得静默截断results、漏掉失败行或把部分结果当全部完成。

## 验证命令

```bash
python3 -m unittest discover -s "<skill>/tests" -v
ruff check "<skill>/scripts" "<skill>/tests"
python3 -m compileall -q "<skill>/scripts"
```
