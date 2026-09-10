(function(root){
'use strict';
var IMAGE_EXT=/\.(?:jpe?g|png|webp)$/i;
var IMAGE_TYPE_BY_EXT={jpg:'image/jpeg',jpeg:'image/jpeg',png:'image/png',webp:'image/webp'};
var ARCHIVE_EXT=/\.(?:zip|tar\.gz|tgz)$/i;
function mib(bytes){return (Number(bytes)/(1024*1024)).toFixed(2);}
function validateImages(files,maxBytes){
  var list=Array.prototype.slice.call(files||[]);
  if(!list.length)return {ok:false,message:'请选择至少一个图片文件。'};
  for(var n=0;n<list.length;n++){
    var file=list[n],type=String(file.type||'').toLowerCase();
    var match=String(file.name||'').match(IMAGE_EXT),ext=match?match[0].slice(1).toLowerCase():'';
    if(!match||IMAGE_TYPE_BY_EXT[ext]!==type)return {ok:false,message:'文件“'+(file.name||'未命名')+'”不是支持的图片；仅接受 JPG、JPEG、PNG、WebP。'};
    if(file.size>maxBytes)return {ok:false,message:'文件“'+file.name+'”大小 '+mib(file.size)+' MiB，超过网页单文件上限 '+mib(maxBytes)+' MiB。请减小文件或使用命令行导入。'};
  }
  return {ok:true,message:list.length+' 个图片文件：'+list.map(function(x){return x.name;}).join('、')};
}
function validateArchive(files,maxBytes){
  var list=Array.prototype.slice.call(files||[]),file=list[0];
  if(list.length!==1)return {ok:false,message:'请选择一个 ZIP、TAR.GZ 或 TGZ 归档。'};
  if(!ARCHIVE_EXT.test(file.name||''))return {ok:false,message:'文件“'+(file.name||'未命名')+'”不是支持的归档；仅接受 ZIP、TAR.GZ、TGZ。'};
  if(file.size>maxBytes)return {ok:false,message:'归档“'+file.name+'”大小 '+mib(file.size)+' MiB，超过应用总上传上限 '+mib(maxBytes)+' MiB。请减小文件或使用命令行导入。'};
  return {ok:true,message:'已选择“'+file.name+'”，大小 '+mib(file.size)+' MiB；应用总上传上限 '+mib(maxBytes)+' MiB。'};
}
function negotiatedChunk(recommended,minimum,maximum){
  return Math.max(Number(minimum),Math.min(Number(recommended),Number(maximum)));
}
function lowerChunk(current,minimum){
  var next=Math.floor(Number(current)/2);
  return next>=Number(minimum)?Math.max(next,Number(minimum)):0;
}
function progressText(offset,total){
  var percent=total?Math.min(100,Math.round(Number(offset)/Number(total)*100)):0;
  return '上传 '+percent+'%（'+mib(offset)+' / '+mib(total)+' MiB）';
}
var api={mib:mib,validateImages:validateImages,validateArchive:validateArchive,negotiatedChunk:negotiatedChunk,lowerChunk:lowerChunk,progressText:progressText};
root.RandomImageUpload=api;
if(typeof module==='object'&&module.exports)module.exports=api;
if(typeof document==='undefined')return;
function errorNode(form){return form.querySelector('[data-upload-error]');}
function setMessage(form,message,isError){
  var h=isError?errorNode(form):form.querySelector('[data-file-summary]');
  if(h)h.textContent=message;
  var e=errorNode(form);if(e){e.hidden=!isError;if(!isError)e.textContent='';}
}
function validator(form,files){
  var max=Number(form.dataset.maxBytes||0);
  if(form.dataset.uploadKind==='archive'&&form.dataset.chunkedEnabled==='true')max=Number(form.dataset.chunkedMaxBytes||max);
  return form.dataset.uploadKind==='archive'?validateArchive(files,max):validateImages(files,max);
}
function check(form,files,clear){
  var result=validator(form,files);setMessage(form,result.message,!result.ok);
  if(!result.ok&&clear){var input=form.querySelector('[data-upload-input]');if(input)input.value='';}
  return result.ok;
}
function restore(button,text){button.disabled=false;button.textContent=text;}
function csrf(){var node=document.querySelector('meta[name="csrf-token"]');return node?node.content:'';}
function selectedTags(form){return Array.prototype.map.call(form.querySelectorAll('input[name="tags"]:checked'),function(x){return x.value;});}
async function fingerprint(file){
  var edge=64*1024,first=file.slice(0,Math.min(edge,file.size));
  var last=file.slice(Math.max(0,file.size-edge),file.size);
  var bytes=new Uint8Array(await new Blob([first,last]).arrayBuffer());
  var digest=await crypto.subtle.digest('SHA-256',bytes);
  return Array.prototype.map.call(new Uint8Array(digest),function(x){return x.toString(16).padStart(2,'0');}).join('');
}
function storageKey(file,digest){return ['ria-upload-v2',file.name,file.size,file.lastModified||0,digest].join(':');}
function savedTask(key){try{return localStorage.getItem(key)||'';}catch(ignore){return '';} }
async function capabilities(base,loginUrl){
  var response=await fetch(base+'/capabilities',{method:'GET',credentials:'same-origin',cache:'no-store'});
  if(response.status===401){location.assign(loginUrl||'./login');throw new Error('');}
  if(!response.ok)throw new Error(await errorFrom(response,'无法读取上传能力，请稍后重试。'));
  return response.json();
}
function saveTask(key,id){try{localStorage.setItem(key,id);}catch(ignore){}}
function forgetTask(key){try{localStorage.removeItem(key);}catch(ignore){}}
async function errorFrom(response,fallback){
  try{var body=await response.json();return body&&body.error&&body.error.message||fallback;}catch(ignore){return fallback;}
}
async function beginOrResume(form,file){
  var digest=await fingerprint(file),key=storageKey(file,digest);
  var base=form.dataset.chunkedUrl,caps=await capabilities(base,form.dataset.loginUrl),task=savedTask(key),response;
  if(!caps.enabled)throw new Error('分片上传当前不可用，请使用普通上传或稍后重试。');
  if(file.size>Number(caps.max_upload_bytes))throw new Error('所选归档超过应用总上传上限，请减小文件。');
  if(task){
    response=await fetch(base+'/'+encodeURIComponent(task),{method:'HEAD',credentials:'same-origin',cache:'no-store'});
    if(response.redirected){location.assign(form.dataset.loginUrl||'./login');throw new Error('');}
    if(response.ok&&Number(response.headers.get('Upload-Length'))===file.size&&response.headers.get('Upload-Fingerprint')===digest){
      return {id:task,url:base+'/'+encodeURIComponent(task),key:key,offset:Number(response.headers.get('Upload-Offset')||0),
        recommended:Number(response.headers.get('Upload-Chunk-Recommended')||caps.recommended_chunk_bytes),minimum:Number(response.headers.get('Upload-Chunk-Min')||caps.min_chunk_bytes),maximum:Number(response.headers.get('Upload-Chunk-Max')||caps.max_chunk_bytes)};
    }
    forgetTask(key);
  }
  var defaultTag=form.querySelector('[name="default_tag"]');
  response=await fetch(base,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify({filename:file.name,size:file.size,fingerprint:digest,default_tag:defaultTag?defaultTag.value:'',tags:selectedTags(form)})});
  if(response.status===401){location.assign(form.dataset.loginUrl||'./login');throw new Error('');}
  if(!response.ok)throw new Error(await errorFrom(response,'无法开始上传，请稍后重试。'));
  var body=await response.json();saveTask(key,body.id);
  return {id:body.id,url:body.location,key:key,offset:Number(response.headers.get('Upload-Offset')||0),recommended:Number(body.recommended_chunk_bytes||caps.recommended_chunk_bytes),minimum:Number(body.min_chunk_bytes||caps.min_chunk_bytes),maximum:Number(body.max_chunk_bytes||caps.max_chunk_bytes)};
}
async function uploadArchive(form,file,status,onTask,signal){
  var task=await beginOrResume(form,file),offset=task.offset;if(onTask)onTask(task);
  var size=negotiatedChunk(task.recommended,task.minimum,task.maximum);
  status.textContent=progressText(offset,file.size);
  while(offset<file.size){
    var end=Math.min(offset+size,file.size),blob=file.slice(offset,end);
    var response=await fetch(task.url,{method:'PATCH',credentials:'same-origin',headers:{'Content-Type':'application/offset+octet-stream','Upload-Offset':String(offset),'X-CSRF-Token':csrf()},body:blob,signal:signal});
    if(response.status===401){location.assign(form.dataset.loginUrl||'./login');return;}
    if(response.status===413){var smaller=lowerChunk(size,task.minimum);if(smaller){size=smaller;continue;}}
    if(response.status===409){var confirmed=Number(response.headers.get('Upload-Offset'));if(Number.isFinite(confirmed)&&confirmed>=0&&confirmed<=file.size){offset=confirmed;status.textContent=progressText(offset,file.size);continue;}}
    if(!response.ok)throw new Error(await errorFrom(response,'分片上传失败，请检查连接后重试。'));
    var confirmedOffset=Number(response.headers.get('Upload-Offset'));
    if(!Number.isFinite(confirmedOffset)||confirmedOffset<=offset||confirmedOffset>file.size)throw new Error('服务端未确认上传进度，请重试。');
    offset=confirmedOffset;status.textContent=progressText(offset,file.size);
  }
  status.textContent='正在安全校验并生成预览';
  var completed=await fetch(task.url+'/complete',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':csrf()},signal:signal});
  if(completed.status===401){location.assign(form.dataset.loginUrl||'./login');return;}
  if(!completed.ok)throw new Error(await errorFrom(completed,'归档校验失败，请检查文件后重试。'));
  var result=await completed.json();forgetTask(task.key);location.assign(result.preview_url);
}
document.addEventListener('DOMContentLoaded',function(){
  document.querySelectorAll('[data-upload-form]').forEach(function(form){
    var input=form.querySelector('[data-upload-input]'),zone=form.querySelector('[data-drop-zone]');if(!input)return;
    input.addEventListener('change',function(){check(form,input.files,true);});
    if(zone){
      ['dragenter','dragover'].forEach(function(name){zone.addEventListener(name,function(e){e.preventDefault();zone.classList.add('is-dragging');});});
      ['dragleave','drop'].forEach(function(name){zone.addEventListener(name,function(e){e.preventDefault();zone.classList.remove('is-dragging');});});
      zone.addEventListener('drop',function(e){var files=e.dataTransfer&&e.dataTransfer.files;if(!files)return;if(!check(form,files,false)){input.value='';return;}input.files=files;input.dispatchEvent(new Event('change',{bubbles:true}));});
    }
  });
  document.addEventListener('submit',function(e){
    var form=e.target;if(!form.matches('[data-upload-form]'))return;
    var input=form.querySelector('[data-upload-input]');if(!input||!check(form,input.files,true)){e.preventDefault();return;}
    if(form.dataset.uploadKind!=='archive'||form.dataset.chunkedEnabled!=='true'||input.files[0].size<=Number(form.dataset.chunkThresholdBytes||0)||!root.fetch||!root.File||!root.Blob||!root.crypto||!root.crypto.subtle)return;
    e.preventDefault();var button=form.querySelector('button[type="submit"],button:not([type])'),original=button.textContent,status=form.querySelector('[data-upload-status]');
    var cancel=form.querySelector('[data-upload-cancel]'),controller=new AbortController(),activeTask=null,cancelled=false;
    button.disabled=true;button.textContent='上传中…';setMessage(form,'',false);status.hidden=false;status.textContent='准备上传；刷新后请重新选择同一文件以继续。';
    if(cancel){cancel.hidden=false;cancel.disabled=false;cancel.onclick=async function(){
      if(cancelled)return;cancelled=true;cancel.disabled=true;controller.abort();
      if(!activeTask){status.hidden=true;cancel.hidden=true;restore(button,original);setMessage(form,'上传已取消。',false);return;}
      try{
        var response=await fetch(activeTask.url,{method:'DELETE',credentials:'same-origin',headers:{'X-CSRF-Token':csrf()}});
        if(response.status===401){location.assign(form.dataset.loginUrl||'./login');return;}
        if(response.status===204||response.status===404||response.status===410){forgetTask(activeTask.key);setMessage(form,'上传已取消。',false);}
        else{throw new Error(await errorFrom(response,'取消请求未完成；可重新选择同一文件查询状态。'));}
      }
      catch(error){setMessage(form,error.message||'取消请求未完成；可重新选择同一文件查询状态。',true);}
      status.hidden=true;cancel.hidden=true;restore(button,original);
    };}
    uploadArchive(form,input.files[0],status,function(task){activeTask=task;},controller.signal).catch(function(error){if(cancelled||error.name==='AbortError')return;if(!error.message)return;setMessage(form,error.message,true);status.hidden=true;restore(button,original);}).finally(function(){if(cancel&&!cancelled)cancel.hidden=true;});
  },true);
});
})(typeof window!=='undefined'?window:globalThis);
