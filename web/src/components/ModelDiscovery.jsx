import {useEffect,useRef,useState} from 'react';
import {roomTargetHeaders} from '../storage/roomScopedStorage.js';

export function ModelDiscovery({upstream,onAdd}) {
  const [models,setModels]=useState(null),[selected,setSelected]=useState([]),[query,setQuery]=useState(''),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  const pending=useRef(null);
  useEffect(()=>{setModels(null);setSelected([]);setMessage('');setBusy(false);return()=>pending.current?.abort();},[upstream.base_url,upstream.api_key,upstream.clear_key,upstream.protocol,upstream.api_key_configured]);
  async function fetchModels(){
    pending.current?.abort();const controller=new AbortController();pending.current=controller;
    setBusy(true);setMessage('正在拉取…');setSelected([]);
    try{
      const response=await fetch('/__serein/settings/models/discover',{method:'POST',signal:controller.signal,headers:{'Content-Type':'application/json',...roomTargetHeaders()},body:JSON.stringify({
        protocol:upstream.protocol,upstream_id:upstream.id,base_url:upstream.base_url,api_key:upstream.api_key||'',clear_key:upstream.clear_key||false})});
      const result=await response.json();
      if(controller.signal.aborted)return;
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'拉取失败，仍可手动填写。');
      setModels(result.models);setQuery('');setMessage(result.models.length?`已拉取 ${result.models.length} 个模型${result.truncated?'（列表已截断）':''}，勾选后添加。`:'上游没有返回可选模型，仍可手动填写。');
    }catch(error){if(!controller.signal.aborted){setModels(null);setMessage(error.message);}}finally{if(!controller.signal.aborted)setBusy(false);}
  }
  const existing=new Set(upstream.models.map(m=>m.upstream_model.trim()));
  const chosen=selected.filter(name=>!existing.has(name));
  const full=upstream.models.length+chosen.length>500;
  return <section className="model-discovery" aria-label="拉取上游模型">
    <div className="settings-actions"><button type="button" disabled={busy||!upstream.base_url.trim()} onClick={fetchModels}>{busy?'正在拉取…':'拉取模型'}</button></div>
    <p className="model-connection-help" role="status">{message||'使用上面填写的地址和密钥拉取；密钥留空时沿用该上游已保存的密钥。拉取不会保存密钥，更换后还需点击“保存上游与模型”。'}</p>
    {models?.length>0&&<>
      <label className="settings-field"><span>搜索模型</span><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="输入模型名筛选"/></label>
      <div className="model-discovery-list">{models.filter(name=>name.toLowerCase().includes(query.toLowerCase())).map(name=><label key={name}>
        <input type="checkbox" checked={existing.has(name)||selected.includes(name)} disabled={existing.has(name)} onChange={e=>setSelected(items=>e.target.checked?[...items,name]:items.filter(x=>x!==name))}/>
        <span>{name}</span>{existing.has(name)&&<small>已添加</small>}
      </label>)}</div>
      <div className="settings-actions"><button type="button" disabled={!chosen.length||full} onClick={()=>{onAdd(chosen);setSelected([]);setMessage('已添加到当前草稿，保存模型设置后生效。');}}>添加所选{chosen.length?`（${chosen.length}）`:''}</button></div>
      {full&&<p className="model-connection-help">每个上游最多保存 500 个模型，请减少选择。</p>}
    </>}
  </section>;
}
