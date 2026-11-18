import {DOMAIN} from '../constants/domain.js';
import {store} from '../stores/research.js';
import {privacy} from '../api/privacy.js';
import {request} from '../api/client.js';
import {studySelect} from '../components/common/study-select.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {esc,fmt} from '../utils/format.js';
import {PRIVACY_STATE_LABELS} from '../types/privacy-enums.js';
import {download} from '../utils/download.js';

const pStatus=x=>`<span class="status ${x}">${PRIVACY_STATE_LABELS[x]||x}</span>`;

function budgetCard(b){
  const pct=Math.min(100,b.total_epsilon?b.spent_epsilon/b.total_epsilon*100:0);
  return `<section class="method-note"><h2 style="border:0;margin:0 0 10px;padding:0">隐私预算台账（同批所有版本共用 · 不可重置）</h2>
  <div class="budget-bar"><i style="width:${pct.toFixed(2)}%"></i></div>
  <p style="margin-bottom:6px">总额 ε=<b>${fmt(b.total_epsilon)}</b> · 已花 ε=<b>${fmt(b.spent_epsilon)}</b> · 剩余 ε=<b>${fmt(b.remaining_epsilon)}</b></p>
  <small>申请在公开前即扣费；并发申请行锁串行、余额不足整笔拒绝；数据更正不恢复额度，公开后撤销也不退费。</small></section>`;
}

function gridView(r){
  const g=r.result;
  const rows=g.grid_counts.map((row,i)=>[g.grid_labels.rows[i],...row]);
  return `<h3>${esc(g.grid_title)}</h3>${table([g.grid_labels.row,...g.grid_labels.cols],rows)}
  <h3>加噪边际与平均秩（仅由加噪计数推导）</h3>
  ${table(['网格组','孔隙率加噪边际','孔隙率平均秩','吸水率加噪边际','吸水率平均秩'],g.grid_labels.rows.map((label,i)=>[label,g.marginals.porosity_noisy[i],fmt(g.ranks.porosity_avg_rank[i]),g.marginals.absorption_noisy[i],fmt(g.ranks.absorption_avg_rank[i])]))}`;
}

function detail(r,role){
  if(!r)return '';
  const actions=[];
  if(r.status==='failed')actions.push(`<button id="dp-retry">重试（沿用已持久化噪声 · 不重复扣费）</button>`);
  if(role==='admin'&&r.status==='ready')actions.push(`<button id="dp-publish">公开前一致性校验并公开 →</button>`);
  if(role==='admin'&&r.status==='published')actions.push(`<button id="dp-revoke" class="secondary">撤销公开（预算不退）</button>`);
  if(r.status==='published')actions.push(`<button id="dp-export">下载公开 CSV（不含原值/精确人数）</button>`);
  return `<section class="worksheet" style="max-width:100%">
  <h2 style="margin-top:0">#${r.id} ${esc(r.name)} ${pStatus(r.status)}</h2>
  <p>扣费台账 #${r.ledger_seq} · ε=${fmt(r.epsilon)} · 扣费时间 ${esc(r.charged_at)}${r.published_at?` · 公开于 ${esc(r.published_at)}`:''}${r.revoked_at?` · 撤销于 ${esc(r.revoked_at)}`:''}</p>
  ${r.error?`<div class="error">计算失败：${esc(r.error)}</div>`:''}
  ${r.result?`<div class="result-intro"><strong>${esc(r.result.interpretation)}</strong><p>${esc(r.result.mechanism)}</p><p>${esc(r.result.caveat)}</p></div>
  <h3>公开指标</h3>${table(['指标','值'],Object.entries(r.result.metrics).map(([k,v])=>[k,fmt(v)]))}
  ${gridView(r)}
  <details><summary>本次发布的方法口径与持久化噪声指纹</summary><pre>${esc(JSON.stringify({seed:r.seed,checksum:r.checksum,result_fingerprint:r.result_fingerprint,artifact_path:r.artifact_path,has_persisted_noise:!!r.noise_draws},null,2))}</pre></details>`:'<p class="method-note">任务排队或计算中，本页每2秒刷新。中断恢复会沿用已持久化噪声，不会重新扣费。</p>'}
  <div class="toolbar">${actions.join('')}<a class="button secondary" href="#privacy">返回发布列表</a></div></section>`;
}

export async function render(id){
  const role=localStorage.getItem('research-role');
  const [budget,releases]=await Promise.all([privacy.budget(),privacy.list()]);
  const r=id?await privacy.get(id):null;
  if(r)store.dpId=r.id;
  return `<div class="eyebrow">06 / PRIVACY LEDGER · DIFFERENTIAL PRIVACY</div>
  <h1>差分隐私公开研究</h1>
  <p class="lead">孔隙率与吸水率按预设固定 10×10 网格联合计数；增删一个试件的 L1 敏感度 Δ=1，全部单元（含空格）加 Laplace(0, 1/ε) 噪声，再由加噪边际推导秩相关。公开内容不含单件原值或精确人数；原内部研究在其他页面保留不变。</p>
  ${budgetCard(budget)}
  ${detail(r,role)}
  <h2>申请差分隐私发布 <small>申请即扣费，扣费后排队由 worker 计算</small></h2>
  <form id="dp-form" class="worksheet">
    ${studySelect()}
    <label>发布名称<input name="name" required minlength="2" value="${esc(DOMAIN.names[0])} · 差分隐私公开"></label>
    <div class="inline-form">
      <label>本次隐私预算 ε（${0.01}–${1.0}，且不超过剩余额度）<input name="epsilon" type="number" step="0.01" min="0.01" max="1" value="0.2" required></label>
    </div>
    <p>噪声尺度 b=Δ/ε=1/ε；ε 越小噪声越大。扣费记录与结果文件指纹不一致时系统禁止公开。</p>
    <button type="submit">扣费并提交发布申请 →</button>
  </form>
  <h2>发布申请与扣费记录</h2>
  ${table(['编号 / 名称','状态','ε','台账序号','扣费时间 (UTC)','公开时间'],releases.map(x=>[{html:`<a href="#privacy/${x.id}">#${x.id} ${esc(x.name)}</a>`},{html:pStatus(x.status)},fmt(x.epsilon),'#'+x.ledger_seq,x.charged_at,x.published_at||'—']))}`;
}

export function bind(refresh){
  bindForm('dp-form',async data=>{
    const r=await privacy.create({study_id:+data.study_id,name:data.name,epsilon:+data.epsilon});
    await refresh();location.hash='privacy/'+r.id;
  });
  const sel=document.getElementById('study-select');if(sel)sel.onchange=()=>{store.studyId=+sel.value;};
  const id=store.dpId;
  const retry=document.getElementById('dp-retry');
  if(retry)retry.onclick=async()=>{try{await privacy.retry(id);await refresh();}catch(e){document.getElementById('notice').textContent=e.message;}};
  const pub=document.getElementById('dp-publish');
  if(pub)pub.onclick=async()=>{try{await privacy.publish(id);await refresh();}catch(e){document.getElementById('notice').textContent=e.message;}};
  const rev=document.getElementById('dp-revoke');
  if(rev)rev.onclick=async()=>{try{await privacy.revoke(id);await refresh();}catch(e){document.getElementById('notice').textContent=e.message;}};
  const exp=document.getElementById('dp-export');
  if(exp)exp.onclick=async()=>{try{download(await request(`/privacy/releases/${id}/export`),'privacy-release-'+id+'.csv');}catch(e){document.getElementById('notice').textContent=e.message;}};
}
