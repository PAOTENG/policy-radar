# 粤创智造合成政策图谱评测语料

## 格式说明（调研结论）

| 格式 | 用途 | 本语料 |
|------|------|--------|
| **Markdown** | RAG 正文主流：保留章节结构，适合按标题分块与嵌入 | `policies/*.md` |
| **YAML frontmatter** | 文档级元数据（地区/文号/截止/金额） | 每个 md 文件头部 |
| **JSON manifest** | 图边、金标冲突、入库映射 | `graph_manifest.json` |
| 纯 txt | 结构弱，不推荐作为主格式 | 未采用 |
| 原始 PDF | 归档用；入库前通常先转 md | 未生成 |

业界常见做法：**Markdown 存正文 + JSON 存元数据/图关系**，而不是把一切塞进纯文本。

## 内容设计

虚构但体例真实的广东省「粤创智造·高新技术企业研发费用补贴」政策族，共 **20** 份文件。

覆盖冲突类型：
- 截止时间冲突（省通知 9-30 vs 深圳 8-15 / 广州 9-10）
- 金额冲突与省–市层级差异（省 500 万 vs 深圳 200 万 / 广州 300 万 / 东莞 450 万）
- 主体门槛冲突（省 注册资本500万+满2年 vs 深圳 200万+满1年）
- 效力冲突（2024 办法被 2026 修订与废止公告替代）
- 互斥/不得重复（叠加享受补充通知、科技金融口径）

图关系类型：`supersedes` / `implements` / `region_child` / `funding_of` / `lists_under` / `amends` / `complements` / `same_program`

## 使用

### 入库 + 建图（已支持）

```text
python scripts/ingest_synthetic_kg_corpus.py
```

写入：`policies`（文档向量）+ `policy_chunks`（分块向量）+ Elasticsearch BM25 + `policy_relations`（manifest 边）。  
`source_url` 形如 `synthetic://yuechuang/P01`，可重复执行（先清旧合成数据）。

### 跑测试用例

```text
python scripts/run_synthetic_kg_tests.py
```

用例文件：`test_cases.json`（图谱补召回 / 冲突门控 / 混合检索 / e2e），映射见 `id_map.json`。

## 字数

各文件汉字约：见 `graph_manifest.json` → `char_counts_approx`（目标每篇数百至一千以上汉字，含完整章节结构）。
