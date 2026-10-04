"""Render an existing comparison report without rerunning or selecting trials."""
import json
import os
from pathlib import Path
import statistics
import sys

source=Path(sys.argv[1]);target=Path(sys.argv[2]);report=json.loads(source.read_text())
rows=report['results'];groups=report['summary'];pairs={}
for row in rows:pairs.setdefault((row['case_id'],row['repeat']),{})[row['architecture']]=row
lines=['# 单Agent与多Agent对照结果','',f"模型配置：`{report['model']}`；模式：`{report['mode']}`；报告完整：{report['complete']}。",
       f"案例数：{len(report['case_ids'])}；每题每组计划尝试：{report['repeats']}次；实际完成：{len(rows)}次。",'',
       '场景通过包含正确阻止过期操作；业务完成另计。所有失败尝试均包含在下面的汇总中。','',
       '| 指标 | 单Agent | 多Agent |','|---|---:|---:|']
for label,key in [('已运行次数','trials'),('场景通过','scenario_passes'),('业务完成','business_completed'),('实际违规执行','illegal_executions'),('违规方案','unsafe_proposals'),('总Token','tokens'),('平均Token','mean_tokens'),('平均模型调用','mean_model_calls'),('耗时中位数（ms）','median_elapsed_ms'),('最长耗时（ms）','max_elapsed_ms')]:
 lines.append(f"| {label} | {groups.get('single',{}).get(key,'—')} | {groups.get('multi',{}).get(key,'—')} |")
lines+=['','## 逐题结果','','| 案例 / 次数 | 单Agent | 多Agent |','|---|---|---|']
for caseid in report['case_ids']:
 for repeat in range(1,report['repeats']+1):
  p=pairs.get((caseid,repeat),{})
  def cell(arch):
   r=p.get(arch)
   return '未运行' if not r else f"{'通过' if r['passed'] else '未通过'} · {r['status']} · {r['tokens']} Token · {r['elapsed_ms']/1000:.1f}s"
  lines.append(f"| {caseid} / {repeat} | {cell('single')} | {cell('multi')} |")
common=[p for p in pairs.values() if len(p)==2 and all(r['passed'] for r in p.values())]
lines+=['','## 双方通过的配对案例','',f'共有{len(common)}对，不包含只有一组通过的案例；不能用这个子集代替完整结果。']
if common:
 lines.append('')
 for arch in ('single','multi'):
  lines.append(f"- {arch}：平均Token {statistics.mean(p[arch]['tokens'] for p in common):.1f}，平均耗时 {statistics.mean(p[arch]['elapsed_ms'] for p in common)/1000:.1f}秒。")
lines+=['','## 失败记录','']
for r in rows:
 if not r['passed']:
  failed=[k for k,v in r['checks'].items() if not v]
  lines.append(f"- {r['case_id']} / {r['architecture']} / 第{r['repeat']}次：{r['status']}；未满足：{', '.join(failed)}；错误：{r['error'] or '无系统异常，见逐步轨迹'}。")
if all(r['passed'] for r in rows):lines.append('本轮已完成的尝试均通过场景断言。')
lines+=['',f"[原始逐题报告]({os.path.relpath(source,target.parent)}) · [实验方法](comparison-method.md)",'', '本报告为小规模合成场景回归，不能据此声称生产成功率或多Agent必然优于单Agent。',
        f"源码指纹：`{report['source_sha256']}`；案例指纹：`{report['cases_sha256']}`。",'']
target.write_text('\n'.join(lines))
print(target)
