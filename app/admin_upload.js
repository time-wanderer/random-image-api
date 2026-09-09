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
  if(file.size>maxBytes)return {ok:false,message:'归档“'+file.name+'”大小 '+mib(file.size)+' MiB，超过网页上限 '+mib(maxBytes)+' MiB。请减小文件或使用命令行导入。'};
  return {ok:true,message:'已选择“'+file.name+'”，大小 '+mib(file.size)+' MiB；网页上限 '+mib(maxBytes)+' MiB。'};
}
var api={mib:mib,validateImages:validateImages,validateArchive:validateArchive};
root.RandomImageUpload=api;
if(typeof module==='object'&&module.exports)module.exports=api;
if(typeof document==='undefined')return;
function errorNode(form){return form.querySelector('[data-upload-error]');}
function setMessage(form,message,isError){
  var h=isError?errorNode(form):form.querySelector('[data-file-summary]');
  if(h)h.textContent=message;
  var e=errorNode(form);
  if(e){e.hidden=!isError;if(!isError)e.textContent='';}
}
function validator(form,files){
  var max=Number(form.dataset.maxBytes||0);
  return form.dataset.uploadKind==='archive'?validateArchive(files,max):validateImages(files,max);
}
function check(form,files,clear){
  var result=validator(form,files);
  setMessage(form,result.message,!result.ok);
  if(!result.ok&&clear){var input=form.querySelector('[data-upload-input]');if(input)input.value='';}
  return result.ok;
}
function restore(button,text){button.disabled=false;button.textContent=text;}
document.addEventListener('DOMContentLoaded',function(){
  document.querySelectorAll('[data-upload-form]').forEach(function(form){
    var input=form.querySelector('[data-upload-input]'),zone=form.querySelector('[data-drop-zone]');
    if(!input)return;
    input.addEventListener('change',function(){check(form,input.files,true);});
    if(zone){
      ['dragenter','dragover'].forEach(function(name){zone.addEventListener(name,function(e){e.preventDefault();zone.classList.add('is-dragging');});});
      ['dragleave','drop'].forEach(function(name){zone.addEventListener(name,function(e){e.preventDefault();zone.classList.remove('is-dragging');});});
      zone.addEventListener('drop',function(e){
        var files=e.dataTransfer&&e.dataTransfer.files;
        if(!files)return;
        if(!check(form,files,false)){input.value='';return;}
        input.files=files;
        input.dispatchEvent(new Event('change',{bubbles:true}));
      });
    }
  });
  document.addEventListener('submit',function(e){
    var form=e.target;
    if(!form.matches('[data-upload-form]'))return;
    var input=form.querySelector('[data-upload-input]');
    if(!input||!check(form,input.files,true)){e.preventDefault();return;}
    if(form.dataset.uploadKind!=='archive'||!root.XMLHttpRequest||!root.FormData)return;
    e.preventDefault();
    var button=form.querySelector('button[type="submit"],button:not([type])'),original=button.textContent,status=form.querySelector('[data-upload-status]');
    var xhr=new XMLHttpRequest();
    button.disabled=true;button.textContent='上传中…';setMessage(form,'',false);status.hidden=false;status.textContent='准备上传…';
    xhr.open('POST',form.action,true);xhr.timeout=30*60*1000;
    xhr.upload.onprogress=function(ev){
      if(ev.lengthComputable){var percent=Math.min(100,Math.round(ev.loaded/ev.total*100));status.textContent='上传 '+percent+'%（'+mib(ev.loaded)+' / '+mib(ev.total)+' MiB）';}
    };
    xhr.upload.onload=function(){status.textContent='正在安全校验并生成预览';};
    xhr.onload=function(){
      var path='';try{path=new URL(xhr.responseURL||'',location.href).pathname;}catch(ignore){}
      if(path.endsWith('/login')){location.assign(xhr.responseURL);return;}
      if(xhr.status===401){location.assign(form.dataset.loginUrl||'./login');return;}
      if(xhr.status>=200&&xhr.status<300){document.open();document.write(xhr.responseText);document.close();return;}
      var gateway=[413,502,503,504,520,522,524].indexOf(xhr.status)>=0;
      setMessage(form,gateway?'上传失败（HTTP '+xhr.status+'）。可能超过上传链路限制，请减小文件或使用命令行导入。':'归档处理失败（HTTP '+xhr.status+'），请检查文件后重试。',true);
      status.hidden=true;restore(button,original);
    };
    xhr.onerror=function(){setMessage(form,'网络中断，归档未完成上传；请检查连接后重试。',true);status.hidden=true;restore(button,original);};
    xhr.ontimeout=function(){setMessage(form,'上传超时，归档未完成处理；请减小文件、检查连接或使用命令行导入。',true);status.hidden=true;restore(button,original);};
    xhr.onabort=function(){setMessage(form,'上传已中断，请重试。',true);status.hidden=true;restore(button,original);};
    xhr.send(new FormData(form));
  },true);
});
})(typeof window!=='undefined'?window:globalThis);
