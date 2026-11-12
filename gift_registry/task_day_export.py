"""V4 original-row accounting and versioned publication, independent of cleanup.

Lock order: export-service -> rows -> jobs. XLSX and hashes are built outside
rows/jobs locks. A source token is checked before atomic publication. Versioned
workbooks are immutable; a lost manifest publication is safely reconciled.
"""
import hashlib
import json
import re
import threading
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .business_day import CHINA, job_day
from .daily_export import export_workbook, NS
from .diagnostics import diagnose
from .sessions import atomic_write

TERMINAL = {'success','failed','skipped','cancelled','manual_required'}


class SourceChanged(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()).hexdigest()


def summarize(job):
    counts = Counter('reused' if i.get('reused_result') else i['state'] if i['state'] in TERMINAL else 'pending'
                     for i in job['items'])
    clicks = {i.get('execution_id') for i in job['items'] if not i.get('reused_result')
              and i.get('execution_id') and i.get('evidence',{}).get('submit_clicked') is True}
    done = sum(i['state'] in TERMINAL for i in job['items'])
    return dict(input=len(job['rows']), done=done, remaining=len(job['rows'])-done,
                actual_submissions=len(clicks), **{k:counts[k] for k in
                ('success','failed','reused','skipped','cancelled','manual_required','pending')})


def build_report(day, jobs, marks, version, exported_at):
    final = all(summarize(j)['remaining']==0 for j in jobs)
    summary = [['业务日期','批次 ID','开始时间','首次完成时间','输入行','真实提交数','成功','失败','历史复用','跳过','取消','需人工核查','待完成','导出版本','导出时间','是否最终版','原批次 ID']]
    detail = [['业务日期','批次 ID','原始行号','row_id','订单号','礼品名称','SKU','execution_id','真实状态','人工展示状态','提交证据',
               '创建时间','开始时间','提交时间','首次完成时间','最新核验时间','最新复核时间','结果说明','原批次 ID','来源执行 ID']]
    success = [['完成时间','订单号','新增内部订单号','礼品','执行编号']]
    failed = [['完成时间','订单号','结果说明','礼品','执行编号']]
    reviews = [['业务日期','批次 ID','执行编号','复核时间','复核结果','最初状态','人工显示']]
    task_marks = {}
    for mark in [*marks.get('events',[]), *marks.get('marks',{}).values()]:
        key = mark.get('task_key')
        if key and (key not in task_marks or mark['at'] >= task_marks[key]['at']): task_marks[key] = mark
    from .registration import task_key, row_signature
    for job in jobs:
        count = summarize(job)
        ended = max((i.get('first_terminal_at', i.get('completed_at', i.get('updated_at',''))) for i in job['items']),default='') if not count['remaining'] else ''
        summary.append([day,job['id'],job.get('created_at',''),ended,count['input'],count['actual_submissions'],
                        *[count[k] for k in ('success','failed','reused','skipped','cancelled','manual_required','pending')],
                        version,exported_at,'最终版' if final else 'partial 非最终版',job.get('origin_batch_id','')])
        for n,(row,item) in enumerate(zip(job['rows'],job['items']),1):
            evidence=item.get('evidence',{}); state=item['state']; execution=item.get('execution_id','')
            mark=marks.get('marks',{}).get(row['id'],{})
            if mark.get('signature') != hashlib.sha256(row_signature(row).encode()).hexdigest(): mark={}
            task_mark=task_marks.get(task_key(row),{})
            if task_mark.get('status')=='success': mark=task_mark
            manual=mark.get('status','')
            completed=item.get('first_terminal_at',item.get('completed_at',''))
            if not completed and state in TERMINAL:
                completed=next((e['at'] for e in item.get('events',[]) if e.get('state') in TERMINAL),'')
            # Never export arbitrary platform exception text, addresses or cookies.
            reason = {'failed':'未通过登记或核验；请在主机查看受保护的执行记录',
                      'manual_required':'提交或核验结果不确定，需人工核查', 'skipped':'历史记录已拦截重复执行',
                      'cancelled':'主机取消；未提交'}.get(state,'')
            proof='已记录点击' if evidence.get('submit_clicked') is True else '无可证实点击记录'
            if item.get('reused_result'): proof='历史复用；本行未提交'
            detail.append([day,job['id'],n,row['id'],row['order_no'],row.get('gift',''),'、'.join(row.get('gift_ids',[])),
                           execution,state,manual,proof,job.get('created_at',''),item.get('started_at',''),item.get('submitted_at',''),
                           completed,item.get('verified_at',''),item.get('reviewed_at',''),reason,
                           job.get('origin_batch_id',''),job.get('parent_execution_id','')])
            if state=='success':
                success.append([completed,row['order_no'],'、'.join(evidence.get('verified_record_ids',[])),row.get('gift',''),execution])
            elif state in {'failed','manual_required'}:
                failed.append([completed,row['order_no'],reason,row.get('gift',''),execution])
            if item.get('review_result'):
                result=item['review_result']
                reviews.append([day,job['id'],execution,result['at'],'success' if result['success'] else '未确认成功','failed',manual])
    sheets=[('成功登记',success),('失败登记',failed),('任务汇总',summary),('任务明细',detail),('后续复核',reviews)]
    payload=export_workbook(sheets)
    validate_report(payload, sum(len(j['rows']) for j in jobs), sum(summarize(j)['input'] for j in jobs))
    return payload, final


def validate_report(payload, expected_rows, summary_input):
    with ZipFile(BytesIO(payload)) as archive:
        if archive.testzip() is not None: raise ValueError('Excel ZIP 校验失败')
        for name in archive.namelist():
            if name.endswith(('.xml','.rels')): ET.fromstring(archive.read(name))
        detail=ET.fromstring(archive.read('xl/worksheets/sheet4.xml'))
        rows=detail.findall(f'{{{NS}}}sheetData/{{{NS}}}row')
        if len(rows)-1 != expected_rows or summary_input != expected_rows:
            raise ValueError('Excel 明细行数对账失败')
        summary=ET.fromstring(archive.read('xl/worksheets/sheet3.xml'))
        total=0
        for row in summary.findall(f'{{{NS}}}sheetData/{{{NS}}}row')[1:]:
            cells={re.sub(r'\d','',c.attrib['r']):c for c in row}
            number=lambda k:int(cells[k].find(f'{{{NS}}}v').text)
            total+=number('E')
            if number('E') != sum(number(k) for k in ('G','H','I','J','K','L','M')):
                raise ValueError('Excel 汇总分类对账失败')
        if total != expected_rows: raise ValueError('Excel 汇总输入行数不一致')
        for row in rows[1:]:
            order=next(c for c in row if c.attrib['r'].startswith('E'))
            if order.attrib.get('t')!='inlineStr': raise ValueError('订单号必须为文本')


class TaskDayExports:
    def __init__(self, jobs, rows, directory, *, clock=lambda: datetime.now(CHINA)):
        self.jobs,self.rows,self.directory,self.clock=jobs,rows,Path(directory),clock
        self.lock=threading.RLock()
        self.manifests=self.directory/'task_day_manifests'
        self.exports=self.directory/'exports'/'v4'

    def token(self, day=None):
        paths=self.paths(day)
        manual=self.rows.path.with_name('manual_status.json')
        return (tuple((p.name,p.stat().st_mtime_ns,p.stat().st_size,self.jobs._storage_epochs.get(p.name,0)) for p in paths),
                (manual.stat().st_mtime_ns,manual.stat().st_size) if manual.exists() else None)

    def paths(self, day=None):
        paths=self.jobs._paths()
        if day is None: return paths
        return [p for p in paths if job_day(self.jobs._snapshot_cache[p.name][1]
                if p.name in self.jobs._snapshot_cache and self.jobs._snapshot_cache[p.name][0] == (p.stat().st_mtime_ns,p.stat().st_size)
                else self.jobs._read_snapshot(p)) == day]

    def snapshot(self, day=None):
        for _ in range(3):
            with self.rows.lock,self.jobs.lock: token=self.token(day); paths=self.paths(day)
            # Authoritative item overlays may change while read. The source
            # token detects that before this copy can be published.
            try:
                jobs=[self.jobs._load(p) for p in paths]
                manual=self.rows.path.with_name('manual_status.json')
                marks=json.loads(manual.read_text(encoding='utf-8')) if manual.exists() else {'marks':{},'events':[]}
            except (FileNotFoundError,ValueError):
                with self.rows.lock,self.jobs.lock:
                    if token==self.token(day): raise
                continue
            with self.rows.lock,self.jobs.lock:
                if token==self.token(day): return jobs,marks,token
        raise SourceChanged('任务仍在更新，请稍后导出')

    def manifest_path(self, day):
        if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day): raise ValueError('业务日期格式不正确')
        datetime.strptime(day,'%Y-%m-%d')
        return self.manifests/(day+'.json')

    def read_manifest(self, day):
        path=self.manifest_path(day)
        if not path.exists(): return dict(version=0,history=[],export_state='pending',business_day=day)
        record=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record,dict) or type(record.get('version')) is not int or not isinstance(record.get('history'),list):
            raise ValueError('导出清单损坏，请保留文件人工核对')
        return record

    def save(self, day, record):
        atomic_write(self.manifest_path(day),json.dumps(record,ensure_ascii=False,indent=2).encode())

    def inventory(self):
        jobs,marks,_=self.snapshot(); days={}; unresolved=[]
        for job in jobs:
            day=job_day(job)
            if day is None:
                unresolved.append(dict(batch_id=job['id'],state='needs_manual_reconciliation')); continue
            days.setdefault(day,[]).append(job)
        result=[]
        for day,batches in sorted(days.items(),reverse=True):
            manifest=self.read_manifest(day); source=digest([batches,marks]); counts=[summarize(j) for j in batches]
            state=manifest['export_state']
            if state != 'error' and manifest.get('source_version')!=source:
                state='needs_refresh' if manifest.get('version') else 'pending'
            result.append(dict(business_day=day,export_state=state,version=manifest['version'],
                               batch_state='open' if any(c['remaining'] for c in counts) else 'finished',
                               input=sum(c['input'] for c in counts),done=sum(c['done'] for c in counts),
                               remaining=sum(c['remaining'] for c in counts),
                               batches=[dict(batch_id=j['id'],**c) for j,c in zip(batches,counts)]))
        return dict(days=result,needs_manual_reconciliation=unresolved)

    def publish(self, day, *, partial=False, batch_id=None):
        with self.lock:
            manifest=self.read_manifest(day)
            try:
                all_jobs,marks,token=self.snapshot(day)
                jobs=[j for j in all_jobs if job_day(j)==day and (not batch_id or j['id']==batch_id)]
                if not jobs: raise ValueError('该业务日或批次没有可核对任务')
                counts=[summarize(j) for j in jobs]; remaining=sum(c['remaining'] for c in counts)
                source=digest([jobs,marks])
                if remaining and not partial:
                    if batch_id: return None
                    if manifest.get('source_version')==source and manifest.get('export_state')=='partial':
                        return None  # Timer must not invalidate an unchanged downloadable partial.
                    manifest.update(export_state=('error' if manifest.get('export_state')=='error' else
                                                  'needs_refresh' if manifest['version'] else 'pending'),batch_state='open',
                                    done=sum(c['done'] for c in counts),remaining=remaining,
                                    batches=[dict(batch_id=j['id'],batch_state='open' if c['remaining'] else 'finished',**c) for j,c in zip(jobs,counts)])
                    self.save(day,manifest); return None
                stamp=self.clock().astimezone(CHINA).isoformat()
                if not batch_id and manifest.get('source_version')==source and manifest.get('export_state') in {'final','partial'}:
                    file=self.exports/manifest['filename']
                    if file.exists() and hashlib.sha256(file.read_bytes()).hexdigest()==manifest.get('sha256'):
                        return file
                version=manifest['version']+1
                payload,final=build_report(day,jobs,marks,version,stamp)
                suffix=('-batch-'+hashlib.sha256(batch_id.encode()).hexdigest()[:12] if batch_id else '')+('' if final else '-partial')
                name=f'gift-registration-{day}{suffix}.xlsx'
                checksum=hashlib.sha256(payload).hexdigest()
                # Keep immutable complete bytes before touching any current name.
                history=self.exports/'history'/f'gift-registration-{day}-v{version}-{checksum[:16]}.xlsx'
                atomic_write(history,payload)
                with self.rows.lock,self.jobs.lock:
                    if token!=self.token(day):
                        manifest.update(export_state='needs_refresh'); self.save(day,manifest)
                        raise SourceChanged('导出期间源记录已变化，保留旧版本并等待重导')
                    destination=self.exports/name
                    # Preserve a pre-existing workbook even if its manifest was
                    # lost. Never silently overwrite historical reports.
                    if destination.exists():
                        old=destination.read_bytes(); old_hash=hashlib.sha256(old).hexdigest()
                        atomic_write(self.exports/'history'/f'preserved-{day}-{old_hash}.xlsx',old)
                    atomic_write(destination,payload)
                    if batch_id: return destination
                    entry=dict(version=version,sha256=checksum,exported_at=stamp,source_version=source,
                               filename=name,history_file=str(history.relative_to(self.exports)))
                    manifest.update(entry,export_state='final' if final else 'partial',
                                    batch_state='finished' if final else 'open',done=sum(c['done'] for c in counts),
                                    remaining=remaining,input=sum(c['input'] for c in counts),error=None,
                                    batches=[dict(batch_id=j['id'],batch_state='open' if c['remaining'] else 'finished',**c) for j,c in zip(jobs,counts)])
                    manifest['history'].append(entry)
                    self.save(day,manifest)
                return destination
            except SourceChanged:
                raise
            except Exception as exc:
                manifest.update(export_state='error',error=type(exc).__name__)
                self.save(day,manifest)
                diagnose(self.directory,'task_export','publish',exc,day)
                raise

    def tick(self):
        # A busy new day must not invalidate the old day's source token.
        days={job_day(self.jobs._read_snapshot(p)) for p in self.jobs._paths()} - {None}
        for day in sorted(days):
            try:
                self.publish(day)
            except (OSError,ValueError):
                pass  # Manifest/diagnostics retain failure.
