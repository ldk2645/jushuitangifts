"""Render the measured JSON artifacts; no estimates for timed-out scenarios."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
docs = root / 'docs'
def read(name): return json.loads((docs/name).read_text(encoding='utf-8'))
def fmt(value, digits=1): return '未完成/未测得' if value is None else f'{value:.{digits}f}'
def mib(value): return fmt(value / 1024**2, 2) if value is not None else '未测得'

rows = []
for label in ['before','after']:
    for scenario,sizes in [('execute',[100,500,1000,2000]),('retry',[100,500,1000])]:
        for size in sizes:
            data=read(f'perf-{label}-{scenario}-{size}.json')
            if label=='after': assert data['complete'] and data.get('exit_code',0)==0
            rows.append((label,scenario,size,data))
assert len(read('benchmark-core-after.json')['results']) == 4
text = '''# 核心修复性能实测

代码：原 main `29c680b` → 修复 `191bfc3`。Windows 11 10.0.26300，Python 3.13.15；临时本地文件、fake Operations、回环 HTTP。真实 Chrome 版本与 DOM 数据另存日志。全部业务提交是模拟调用，没有真实平台等待。

Python 测量开启 tracemalloc，峰值为 Python 分配内存，并非进程总 RSS；这会增加处理时间。每个完整执行场景包含 20 次状态 HTTP 读取；重登场景发送 3 次相同集合请求以检查重复请求。分位值按脚本上分位取样，20/3 个样本不足以代表生产稳定性，p95/p99 可能等于最大样本。不要将其当作生产 SLA。

旧版大规模场景设置 60 秒进程时间预算（包含准备合成夹具），超时后保留最后一次完整采样，故采样耗时可能小于 60 秒。未完成场景没有完成总时长或 HTTP 分位，禁止外推。重登 1000 条基线曾受会话暂停影响，该受影响结果已用新的有界实测替换。

## 完整执行和状态 HTTP

|版本|单量|完成模拟提交|采样/完成秒数|HTTP p50 ms|p95 ms|p99 ms|
|---|---:|---:|---:|---:|---:|---:|
'''
for label,scenario,size,d in rows:
    if scenario!='execute':continue
    text+=f"|{label}{'' if d['complete'] else '（预算到期）'}|{size}|{d.get('submissions',0)}|{fmt(d.get('elapsed_seconds'),2)}|{fmt(d.get('p50'))}|{fmt(d.get('p95'))}|{fmt(d.get('p99'))}|\n"
text+='''
## 批量重登 HTTP 受理

|版本|失败集合|首个请求 ms|p50 ms|p95 ms|p99 ms|HTTP 状态|实际模拟提交|
|---|---:|---:|---:|---:|---:|---|---:|
'''
for label,scenario,size,d in rows:
    if scenario!='retry':continue
    latency=d.get('http_ms',[])
    text+=f"|{label}{'' if d['complete'] else '（预算到期）'}|{size}|{fmt(latency[0] if latency else None)}|{fmt(d.get('p50'))}|{fmt(d.get('p95'))}|{fmt(d.get('p99'))}|{','.join(map(str,d.get('http_status',[]))) or '未取得响应'}|{d.get('submissions',0)}|\n"
text+='''
修复版重复请求返回同一持久化批次，各场景实际模拟提交数恰好等于集合大小。accepted=true 不是平台成功。旧版 100 条场景的重复请求可能返回 400，具体状态保留在原始文件。

## 存储、锁和内存

读取/写入字节含受观测的任务、增量、历史与批次文件。主任务 JSON 次数单独列出；这些不是文件系统全部 I/O 或系统缓存的物理磁盘统计。未完成基线的“最大持锁”仅包含已释放的锁，仍在持有的长锁不在该值内，不能把 0 理解为没有占锁。

|版本/场景|单量|主任务读/写次数|JSON 读取 MiB|JSON 写入 MiB|最大已完成持锁 ms|Python 峰值 MiB|
|---|---:|---:|---:|---:|---:|---:|
'''
for label,scenario,size,d in rows:
    held=d.get('metrics',{}).get('lock_max_hold_seconds')
    text+=f"|{label}/{scenario}{'' if d['complete'] else '*'}|{size}|{d.get('full_job_reads','?')}/{d.get('full_job_writes','?')}|{mib(d.get('json_read_bytes'))}|{mib(d.get('json_write_bytes'))}|{fmt(held*1000 if held is not None else None)}|{mib(d.get('peak_python_bytes'))}|\n"
text+='''
逐行全批次重写已替换为单项增量，完成时整理为完整快照。最终整理的读取/编码在锁外执行，落盘前比较版本，避免长时间阻塞队列。JSON 受理清单仍逐项更新；1000 条批次的总写入量与队列等待仍值得后续优化，见原始结果中的 queue_max_wait_seconds。当前先满足快速、幂等受理与单浏览器串行安全，不把排队时间隐藏成“成功”。

## 前端真实浏览器测量

|版本|粘贴行数|粘贴处理 ms|DOM 总节点|挂载数据行|
|---|---:|---:|---:|---:|
'''
for label in ['before','after']:
    for d in read(f'benchmark-dom-{label}.json'):
        text+=f"|{label}|{d['rows']}|{fmt(d['paste_ms'])}|{d['dom_nodes']}|{d['mounted_rows']}|\n"
text+='''
修复版还验证了跨页全量复制、范围填充、撤销、筛选、焦点导航、保存和无变化状态节点复用。分页只限制挂载节点，不删减底层行或复制范围。

## 原始结果与重跑

- `perf-before/after-execute-N.json`：100/500/1000/2000 单模拟执行、HTTP 样本、JSON 次数/字节、锁与内存。
- `perf-before/after-retry-N.json`：100/500/1000 条重登的三次重复 HTTP、批次阶段与完整模拟执行。
- `benchmark-core-before/after.json`：未接入平台时的 100/500/1000/2000 行受理及 20 次状态 HTTP；与完整执行场景分开。
- `benchmark-dom-before/after.json`：浏览器粘贴处理时间和 DOM 数。
- `tools/benchmark_matrix.py` 在 `.tmp/core-baseline` 需要原 main 的隔离 archive；使用 `git archive main` 时先确认 main 仍是上述基线，不能借用其他分支。
- `python tools/summarize_core_benchmarks.py` 从已保存 JSON 重新生成本文。
'''
(docs/'main-core-performance.md').write_text(text,encoding='utf-8')
print('Wrote source-backed performance report.')
