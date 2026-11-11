/**
 * 随手改 · HTML 可视化编辑器（SnapEdit）运行时打包器
 *
 * 设计目标：将离线版「随手改-HTML编辑器」的完整能力（CSS 样式 + UI 骨架 + 交互脚本）
 * 打包成一段可注入 iframe srcdoc 的独立 HTML。集成方只需传入待编辑的报告 HTML 片段，
 * 即可在 iframe 内对报告进行可视化编辑（改文字、调样式、复制/上移/下移/删除等），
 * 并可随时导出/还原/切换动态预览。
 *
 * 使用场景：ReportView 在 pageSource === 'RM' 且 platformSource === 'SKILL_CONFIG' 时调用
 * buildSnapEditorHtml(renderedHtmlContent) 生成编辑器页面，替代原有只读 iframe 预览。
 *
 * 注意：
 *  1. 为保证编辑器脚本可运行（允许编辑/动态预览），注入该 HTML 的 iframe 需带
 *     sandbox="allow-scripts allow-same-origin"（无 sandbox 亦可，编辑器外壳同样源于
 *     srcdoc，与父页面同源）。
 *  2. 传入的报告 HTML 会被当作初始文档载入，编辑过程全程在本机 iframe 内完成。
 *
 */


const SNAP_EDITOR_STYLE = `
*{box-sizing:border-box}
html,body{margin:0;padding:0;height:100%;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;background:#FAFAF9;color:#292524;overflow:hidden}


/* ===== 顶栏 ===== */
#shell{display:flex;flex-direction:column;height:100vh;width:100vw}
.bar{display:flex;align-items:center;gap:10px;padding:10px 16px;background:linear-gradient(135deg,#1E1B4B 0%,#312E81 100%);flex-shrink:0;z-index:100;box-shadow:0 2px 12px rgba(30,27,75,.35)}
.brand{display:flex;align-items:baseline;gap:8px;margin-right:8px;user-select:none}
.brand b{color:#fff;font-size:16px;letter-spacing:1px}
.brand i{color:#A5B4FC;font-size:11px;font-style:normal;letter-spacing:.5px}
.brand .dot{width:8px;height:8px;border-radius:50%;background:#EA580C;align-self:center;box-shadow:0 0 8px rgba(234,88,12,.8)}
.bar button{min-height:32px;padding:6px 14px;border:1px solid rgba(255,255,255,.22);background:rgba(255,255,255,.06);border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;color:#E0E7FF;transition:all .15s;white-space:nowrap}
.bar button:hover:not(:disabled){background:rgba(255,255,255,.14);border-color:rgba(255,255,255,.4)}
.bar button:disabled{opacity:.35;cursor:not-allowed}
.bar button.primary{background:#EA580C;border-color:#EA580C;color:#fff;box-shadow:0 2px 8px rgba(234,88,12,.4)}
.bar button.primary:hover:not(:disabled){background:#C2410C;border-color:#C2410C}
.bar .spacer{flex:1}
.pill{color:#C7D2FE;font-size:12px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:6px 12px;background:rgba(255,255,255,.08);border-radius:999px;border:1px solid rgba(255,255,255,.15)}
#badge{display:flex;align-items:center;gap:6px;color:#C7D2FE;font-size:12px;padding:6px 10px;border-radius:999px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.15);white-space:nowrap}
#badge::before{content:"";width:7px;height:7px;border-radius:50%;background:#6EE7B7}
#badge.dirty::before{background:#FBBF24}
.switch{display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;font-size:13px;color:#C7D2FE;padding:5px 12px 5px 6px;border:1px solid rgba(255,255,255,.18);border-radius:999px;background:rgba(255,255,255,.05);transition:all .15s}
.switch:has(input:checked){color:#fff;border-color:#EA580C;background:rgba(234,88,12,.25)}
.switch input{width:34px;height:18px;cursor:pointer;margin:0;accent-color:#EA580C}


/* ===== 画布 ===== */
#stage{flex:1;position:relative;overflow:hidden;background-color:#FAFAF9;background-image:radial-gradient(#E0E0EC 1px,transparent 1px);background-size:22px 22px}
iframe#view{width:100%;height:100%;border:none;background:#fff;display:block}
#tip{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);background:rgba(30,27,75,.92);color:#E0E7FF;padding:8px 18px;border-radius:999px;font-size:12px;z-index:9997;pointer-events:none;opacity:0;transition:opacity .2s;white-space:nowrap}
#banner{position:absolute;top:14px;left:50%;transform:translateX(-50%);display:none;align-items:center;gap:10px;background:#FFF7ED;border:1px solid #FDBA74;color:#9A3412;padding:8px 14px;border-radius:10px;font-size:13px;box-shadow:0 8px 24px rgba(154,52,18,.15);z-index:9997;max-width:calc(100vw - 24px)}
#banner button{border:none;background:#EA580C;color:#fff;padding:5px 12px;border-radius:7px;font-size:12.5px;cursor:pointer}
#banner button:hover{background:#C2410C}
#banner .x{cursor:pointer;color:#C2410C;padding:2px 6px;border-radius:6px}
#banner .x:hover{background:rgba(194,65,12,.12)}


/* ===== 覆盖层 ===== */
.ovl{position:fixed;pointer-events:none;z-index:9998;border-radius:3px;display:none}
#hoverBox{border:2px solid #6366F1;background:rgba(99,102,241,.06)}
#pickBox{border:2px solid #EA580C;background:rgba(234,88,12,.06)}


/* ===== 块操作条 ===== */
#blockBar{position:fixed;display:none;gap:4px;background:#1E1B4B;border-radius:10px;padding:5px;z-index:10000;box-shadow:0 10px 30px rgba(30,27,75,.35)}
#blockBar button{border:none;background:transparent;color:#E0E7FF;font-size:12.5px;padding:5px 10px;border-radius:7px;cursor:pointer;white-space:nowrap}
#blockBar button:hover:not(:disabled){background:rgba(255,255,255,.14)}
#blockBar button.warn:hover{background:#BE123C}
#blockBar button[data-act="ai"]{display:inline-flex;align-items:center;gap:4px;background:rgba(234,88,12,.25);border:1px solid rgba(234,88,12,.5);color:#FED7AA}
#blockBar button[data-act="ai"]:hover:not(:disabled){background:rgba(234,88,12,.4)}
#blockBar button[data-act="ai"]:disabled{opacity:.4;cursor:not-allowed}


/* ===== AI 调试浮层（浏览器窗口居中弹窗） ===== */
#aiMask{position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(15,15,35,.45);backdrop-filter:blur(2px);display:none;z-index:10001}
#aiPanel{position:fixed;top:5%;left:50%;transform:translate(-50%);display:none;flex-direction:column;gap:10px;background:#fff;border:1px solid #E0E0EC;border-radius:14px;box-shadow:0 20px 50px rgba(30,27,75,.22);padding:14px;width:800px;max-width:calc(100vw - 16px);max-height:calc(100vh - 32px);overflow:auto;z-index:10002;font-size:13px}


#aiPanel .aiHead{display:flex;justify-content:space-between;align-items:center;font-weight:600;color:#1E1B4B;font-size:13.5px}
#aiPanel .aiHead span{cursor:pointer;color:#9C9CB0;font-size:15px;padding:2px 7px;border-radius:6px}
#aiPanel .aiHead span:hover{background:#F0F0F5;color:#1E1B4B}
#aiPanel textarea{width:100%;min-height:60px;max-height:120px;resize:vertical;border:1px solid #E0E0EC;border-radius:9px;padding:8px 10px;font-size:13px;font-family:inherit;color:#292524;background:#FAFAF9}
#aiPanel textarea:focus{outline:none;border-color:#EA580C}
#aiPanel .aiRow{display:flex;justify-content:end;align-items:center;gap:10px}
#aiPanel .aiRow .aiHint{flex:1;font-size:11.5px;color:#9C9CB0;word-break:break-all;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#aiPanel .aiSend{background:#EA580C;border:none;color:#fff;padding:7px 16px;border-radius:8px;font-size:13px;cursor:pointer;white-space:nowrap}
#aiPanel .aiSend:hover:not(:disabled){background:#C2410C}
#aiPanel .aiSend:disabled{opacity:.5;cursor:not-allowed}
#aiLoading{display:none;align-items:center;gap:8px;color:#57534E;font-size:12px;padding:8px 10px;background:#FFF7ED;border:1px dashed #FDBA74;border-radius:9px}
#aiLoading.on{display:flex}
#aiLoading .spin{width:14px;height:14px;border:2px solid #FDBA74;border-top-color:transparent;border-radius:50%;animation:aiSpin .7s linear infinite;flex-shrink:0}
@keyframes aiSpin{to{transform:rotate(360deg)}}
#aiBar{display:flex;align-items:center;gap:8px;color:#57534E;font-size:12px;padding:8px 10px;background:#F5F5F4;border:1px solid #E0E0EC;border-radius:9px;min-height:34px;max-height:120px;overflow:auto;white-space:pre-wrap;word-break:break-word}
#aiBar.hidden{display:none}
/* 预览区域：展示 AI 返回结果，供用户确认替换 */
#aiPreview{display:none;flex-direction:column;gap:8px;border:1px solid #E0E0EC;border-radius:9px;background:#FAFAF9;overflow:hidden}
#aiPreview.on{display:flex}
#aiPreview .pvHead{display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:#F0F0F5;font-size:12px;color:#48485C;font-weight:600}
#aiPreview .pvHead span{cursor:pointer;color:#9C9CB0;font-size:14px;padding:0 4px;border-radius:4px}
#aiPreview .pvHead span:hover{background:#E4E4ED;color:#1E1B4B}
#aiPreview .pvBody{overflow:auto;padding:10px;font-size:12px;color:#444;line-height:1.5}
/* AI 预览包裹容器：隔离 reportHtml 注入的 <style>，避免其类名规则污染编辑器外壳 UI；
   容器仅提供普通块级渲染上下文，内部按 reportHtml 原始 CSS 渲染（与 #view 语义一致） */
#aiPreview .snap-ai-preview-scope{display:block;box-sizing:border-box}



#aiPanel .aiBtn{border:1px solid #E0E0EC;background:#fff;color:#48485C;padding:7px 14px;border-radius:8px;font-size:13px;cursor:pointer}
#aiPanel .aiBtn:hover{background:#F0F0F5}
#aiPanel .aiBtn.primary{background:#EA580C;border-color:#EA580C;color:#fff}
#aiPanel .aiBtn.primary:hover{background:#C2410C}
#aiPanel .aiBtn:disabled{opacity:.5;cursor:not-allowed}




/* ===== 选中文字工具条 ===== */
#textBar{position:fixed;display:none;align-items:center;gap:2px;background:#1E1B4B;border-radius:10px;padding:5px;z-index:10001;box-shadow:0 10px 30px rgba(30,27,75,.35)}
#textBar button{border:none;background:transparent;color:#E0E7FF;font-size:13px;min-width:30px;padding:5px 7px;border-radius:7px;cursor:pointer}
#textBar button:hover{background:rgba(255,255,255,.14)}
#textBar label{display:grid;place-items:center;width:30px;height:28px;border-radius:7px;cursor:pointer;font-size:13px;color:#E0E7FF;position:relative}
#textBar label:hover{background:rgba(255,255,255,.14)}
#textBar label input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
#textBar .sep{width:1px;height:18px;background:rgba(255,255,255,.2);margin:0 3px}


/* ===== 颜色面板 ===== */
#palette{position:fixed;display:none;background:#fff;border:1px solid #E0E0EC;border-radius:14px;box-shadow:0 20px 50px rgba(30,27,75,.22);padding:16px;width:280px;z-index:10002;font-size:13px}
#palette h4{margin:0 0 12px;font-size:13px;color:#1E1B4B;display:flex;justify-content:space-between;align-items:center}
#palette h4 span{cursor:pointer;color:#9C9CB0;font-size:15px;padding:2px 7px;border-radius:6px}
#palette h4 span:hover{background:#F0F0F5;color:#1E1B4B}
.crow{display:flex;align-items:center;gap:8px;margin-bottom:9px}
.crow em{font-style:normal;width:48px;color:#48485C;flex-shrink:0;font-size:12.5px}
.crow input[type=color]{width:34px;height:28px;border:1px solid #E0E0EC;border-radius:7px;padding:1px;cursor:pointer;background:#fff}
.crow input[type=text]{flex:1;min-width:0;border:1px solid #E0E0EC;border-radius:7px;padding:5px 8px;font-size:12px;font-family:ui-monospace,monospace;color:#292524}
.crow input[type=text]:focus{outline:none;border-color:#4338CA}
.crow .clr{border:none;background:transparent;color:#9C9CB0;cursor:pointer;font-size:13px;padding:3px 6px;border-radius:6px}
.crow .clr:hover{background:#F0F0F5;color:#292524}
.pbtn{border:1px solid #E0E0EC;background:#fff;border-radius:7px;padding:5px 10px;font-size:12.5px;cursor:pointer;color:#292524}
.pbtn:hover{border-color:#4338CA;color:#4338CA}
#swatches{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px;padding-top:12px;border-top:1px solid #ECECF2}
.sw{width:22px;height:22px;border-radius:6px;cursor:pointer;border:1px solid rgba(0,0,0,.1);transition:transform .1s}
.sw:hover{transform:scale(1.2);box-shadow:0 2px 8px rgba(0,0,0,.25)}


/* ===== Toast ===== */
#toast{position:fixed;right:18px;bottom:18px;background:#1E1B4B;color:#E0E7FF;padding:10px 18px;border-radius:10px;font-size:13px;z-index:10005;opacity:0;transform:translateY(8px);transition:all .25s;pointer-events:none;box-shadow:0 10px 30px rgba(30,27,75,.35)}
#toast.on{opacity:1;transform:translateY(0)}


@media (max-width:760px){
  .bar{flex-wrap:wrap;gap:8px;padding:8px 10px}
  .bar .spacer{display:none}
  .pill{order:10;max-width:none;flex:1 1 100%}
  #badge{order:11}
}


/* ===== 临时屏蔽：当前仅保留「AI 调试」，其余编辑工具先不展示 ===== */
header.bar{display:none !important}
#textBar, #palette{display:none !important}
#blockBar button:not([data-act="ai"]){display:none !important}
#blockBar #btnAi{order:-1}
#tip{display:none !important}
#banner{display:none !important}
`;



/* 顶栏 / 画布 / 覆盖层 / 工具条 / 色板 / Toast 等 UI 骨架（去掉了文件打开/拖拽），由 buildSnapEditorHtml 拼装 */
function buildShellBody(): string {
  return `
<div id="shell">
  <header class="bar">
    <span class="brand"><span class="dot"></span><b>随手改</b><i>SnapEdit · HTML 可视化编辑器</i></span>
    <span class="pill" id="fileName">报告编辑</span>
    <span id="badge">未修改</span>
    <span class="spacer"></span>
    <label class="switch"><input type="checkbox" id="tglEdit" disabled checked><span>编辑模式</span></label>
    <label class="switch" title="让页面里的 JS 跑起来，用于翻页 / 动画类文件。仅对可信文件开启。"><input type="checkbox" id="tglScript" disabled><span>动态预览</span></label>
    <button id="btnUndo" disabled title="撤销上一步（最多 10 步）">⌫ 撤销</button>
    <button id="btnReset" disabled>⟲ 还原</button>
    <button id="btnSave" disabled>⬇ 导出</button>
  </header>
  <main id="stage">
    <iframe id="view" sandbox="allow-same-origin"></iframe>
    <div id="tip">单击文字直接改 · 拖选文字调样式 · 双击 SVG 图文字可改 · 点色块可复制 / 删除 / 换色</div>
    <div id="banner">⚡ 内容似乎没有完整显示，页面可能依赖脚本渲染<button id="bannerBtn">开启动态预览</button><span class="x" id="bannerClose">✕</span></div>
  </main>
</div>


<div id="hoverBox" class="ovl"></div>
<div id="pickBox" class="ovl"></div>


<div id="blockBar">
  <button data-act="color">🎨 颜色</button>
  <button data-act="dup">⧉ 复制</button>
  <button data-act="up">↑ 上移</button>
  <button data-act="down">↓ 下移</button>
  <button data-act="ai" id="btnAi" title="让 AI 按描述调试当前选中块">✨ AI 调试</button>
  <button data-act="del" class="warn">🗑 删除</button>
</div>


<div id="aiMask"></div>
<div id="aiPanel">
  <div class="aiHead">✨ AI 调试选中块<span id="aiClose">✕</span></div>


  <textarea id="aiInput" placeholder="描述你想如何调整这块内容，例如：把标题改成蓝色加粗，金额数字加粗并加大，卡片背景换成浅灰…"></textarea>


  <div id="aiLoading"><span class="spin"></span><span>AI 正在调试，请稍候…</span></div>
  <div id="aiBar" class="hidden"></div>
  <div class="aiRow">
    <button class="aiSend" id="aiSend">发送</button>
    <button class="aiBtn primary" id="aiApply" disabled>确认替换</button>
  </div>
  <div id="aiPreview">
    <div class="pvHead">AI 返回结果预览<span id="aiPreviewClose">✕</span></div>
    <div class="pvBody" id="aiPreviewBody"></div>
  </div>
</div>



<div id="textBar">


  <button data-cmd="bold"><b>B</b></button>
  <button data-cmd="italic"><i>I</i></button>
  <button data-cmd="underline"><u>U</u></button>
  <button data-cmd="strikeThrough"><s>S</s></button>
  <span class="sep"></span>
  <label title="文字颜色">🅰<input type="color" id="selFore" value="#EA580C"></label>
  <label title="背景高亮">🖍<input type="color" id="selBack" value="#FDE68A"></label>
  <span class="sep"></span>
  <button data-fs="-1" title="缩小字号">A−</button>
  <button data-fs="1" title="放大字号">A＋</button>
</div>


<div id="palette">
  <h4>调整颜色 <span id="palClose">✕</span></h4>
  <div class="crow"><em>文字</em><input type="color" id="pText"><input type="text" id="pTextHex" placeholder="#333333"><button class="clr" data-p="color" title="清除">✕</button></div>
  <div class="crow"><em>背景</em><input type="color" id="pBg"><input type="text" id="pBgHex" placeholder="#FFFFFF"><button class="clr" data-p="backgroundColor" title="清除">✕</button></div>
  <div class="crow"><em>边框</em><input type="color" id="pBd"><input type="text" id="pBdHex" placeholder="#DDDDDD"><button class="clr" data-p="borderColor" title="清除">✕</button></div>
  <div class="crow"><em>样式</em><button class="pbtn" id="pBold"><b>B</b> 加粗</button><button class="pbtn" data-efs="-1">A−</button><button class="pbtn" data-efs="1">A＋</button></div>
  <div id="swatches"></div>
</div>


<div id="toast"></div>
`;
}


/* 编辑器交互脚本（IIFE）。相比原版，去掉「打开文件 / 拖拽载入」入口，改为自动载入 INITIAL_HTML。 */
const SNAP_EDITOR_SCRIPT = `
(function(){
"use strict";
/* ---------- 状态 ---------- */
const $ = id => document.getElementById(id);
const view = $("view"), stage = $("stage"), tip = $("tip");
const hoverBox = $("hoverBox"), pickBox = $("pickBox");
const blockBar = $("blockBar"), textBar = $("textBar"), palette = $("palette");
const aiPanel = $("aiPanel"), aiInput = $("aiInput"), aiSend = $("aiSend");
const aiLoading = $("aiLoading"), aiBar = $("aiBar");
const aiMask = $("aiMask");




let rawOriginal = "__INITIAL_HTML__";  // 打开时的原始 HTML（运行时注入）
let docHTML = rawOriginal;             // 当前权威版本
let doctype = "<!DOCTYPE html>";
let fileName = "report-edit.html";
let undoStack = [];                    // 最多 10 步
let vdoc = null;                       // iframe document
let picked = null;                     // 当前选中的块
let editingEl = null;                  // 正在编辑文字的元素
let editSnapshotPending = false;
let dirty = false;
let reportStyleCache = "";           // 初始化时一次性提取的 reportHtml <style>，供 AI 预览复用


// 从 INITIAL_HTML 提取 doctype
(function(){ const m = rawOriginal.match(/^\\s*<!doctype[^>]*>/i); doctype = m ? m[0].trim() : "<!DOCTYPE html>"; })();


const SWATCH = ["#1E1B4B","#4338CA","#6366F1","#EA580C","#C2410C","#0D9488","#14B8A6","#9D174D",
                "#D97706","#FDE68A","#292524","#57534E","#A8A29E","#FFFFFF","#F5F5F4","#FAFAF5"];


/* ---------- 工具 ---------- */
function toast(msg){ const t=$("toast"); t.textContent=msg; t.classList.add("on"); clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("on"),2200); }
function setDirty(d){ dirty=d; const b=$("badge"); b.textContent=d?"已修改":"未修改"; b.classList.toggle("dirty",d); }
function serialize(){
  if(!vdoc) return docHTML;
  let html = vdoc.documentElement.outerHTML
    .replace(/ contenteditable="(true|plaintext-only)"/g,"")
    .replace(/ spellcheck="false"/g,"");
  return doctype + "\\n" + html;
}
function pushUndo(){
  undoStack.push(docHTML);
  if(undoStack.length>10) undoStack.shift();
  $("btnUndo").disabled = false;
}
function commit(){
  docHTML = serialize();
  setDirty(docHTML !== rawOriginal);
  if(docHTML !== rawOriginal) window.parent && window.parent.postMessage({type:"SNAP_EDITOR_CHANGED"}, "*");
}
let _cmH = null;
function commitSoon(){ clearTimeout(_cmH); _cmH = setTimeout(commit, 300); }


/* ---------- 渲染 ---------- */
function render(){
  clearUI();
  const scripts = $("tglScript").checked;
  view.setAttribute("sandbox", scripts ? "allow-scripts allow-same-origin" : "allow-same-origin");
  view.srcdoc = docHTML;
}
view.addEventListener("load", () => {
  try{ vdoc = view.contentDocument; }catch(e){ vdoc=null; return; }
  if(!vdoc) return;
  try{ vdoc.execCommand("styleWithCSS", false, true); }catch(e){}
  bindViewer();
  updateTip();
});


function editableOn(){ return $("tglEdit").checked && !$("tglScript").checked && vdoc; }
function updateTip(){
  if(editableOn()){ tip.style.opacity="1"; clearTimeout(tip._h); tip._h=setTimeout(()=>tip.style.opacity="0",5000); }
  else tip.style.opacity="0";
}


/* ---------- 视图内交互 ---------- */
function bindViewer(){
  const win = view.contentWindow;
  vdoc.addEventListener("mouseover", e => {
    if(!editableOn()) return;
    const el = resolve(e.target);
    if(el && el!==picked && el!==editingEl) showBox(hoverBox, el); else hideEl(hoverBox);
  }, true);
  vdoc.addEventListener("mouseout", ()=> hideEl(hoverBox), true);
  vdoc.addEventListener("click", e => {
    if(!editableOn()) return;
    e.preventDefault(); e.stopPropagation();
    const el = resolve(e.target);
    if(!el){ deselect(); return; }
    /* 当前仅保留「选中块」用于 AI 调试；文字直接改 / SVG 弹窗改等编辑能力先屏蔽 */
    pickBlock(el);
  }, true);
  vdoc.addEventListener("dblclick", e => {
    if(!editableOn()) return;
    e.preventDefault(); e.stopPropagation();
    /* 双击编辑已屏蔽 */
  }, true);


  vdoc.addEventListener("paste", e => {           // 粘贴只留纯文本
    if(!editingEl) return;
    e.preventDefault();
    const t = (e.clipboardData||{}).getData ? e.clipboardData.getData("text/plain") : "";
    if(t) vdoc.execCommand("insertText", false, t);
  }, true);
  vdoc.addEventListener("input", e => {
    if(editingEl && editSnapshotPending){ pushUndo(); editSnapshotPending=false; }
    if(editingEl) commitSoon();
  }, true);
  vdoc.addEventListener("selectionchange", onSelChange);
  win.addEventListener("scroll", reposition, true);
  win.addEventListener("resize", reposition);
  vdoc.addEventListener("keydown", e=>{
    if(e.key==="Escape") deselect();
  }, true);
  checkBlank();
}


/* 脚本渲染页检测：静态预览下几乎没内容 → 提示开动态预览 */
function checkBlank(){
  if($("tglScript").checked){ $("banner").style.display="none"; return; }
  setTimeout(()=>{
    if(!vdoc || !vdoc.body) return;
    const textLen = (vdoc.body.innerText||"").trim().length;
    const hasScript = /<script[\\s>]/i.test(docHTML);
    $("banner").style.display = (hasScript && textLen < 40) ? "flex" : "none";
  }, 300);
}
$("bannerBtn").onclick = ()=>{ $("banner").style.display="none"; $("tglScript").checked=true; $("tglScript").dispatchEvent(new Event("change")); };
$("bannerClose").onclick = ()=> $("banner").style.display="none";


/* 目标解析：仅允许选中「.js-view-edit」容器内部的可编辑块，其余区域不可选中 */
const SKIP = new Set(["HTML","BODY","HEAD"]);
function resolve(t){
  if(!t || SKIP.has(t.tagName)) return null;
  // 仅当目标元素位于可编辑区（js-view-edit 容器）内时才允许选中，用于 AI 调试
  if(!t.closest(".js-view-edit")) return null;
  return t;
}
function isTextEl(el){
  if(["IMG","VIDEO","HR","SVG","CANVAS","IFRAME","INPUT","BUTTON","SELECT","TEXTAREA"].includes(el.tagName)) return false;
  if(el.namespaceURI === "http://www.w3.org/2000/svg") return false;
  for(const n of el.childNodes){ if(n.nodeType===3 && n.textContent.trim()) return true; }
  return false;
}
function isSvgText(el){ return el.namespaceURI === "http://www.w3.org/2000/svg" && ["text","tspan"].includes(el.tagName.toLowerCase()); }


/* ---------- SVG 文字编辑（弹窗改） ---------- */
function editSvgText(el){
  const old = el.textContent;
  const v = prompt("修改 SVG 图中文字：", old);
  if(v === null || v === old) return;
  pushUndo(); el.textContent = v; commit(); reposition();
  toast("SVG 文字已修改");
}


/* ---------- 文字编辑 ---------- */
function beginEdit(el){
  if(editingEl === el) return;
  endEdit(); unpick();
  editingEl = el;
  editSnapshotPending = true;
  el.setAttribute("contenteditable","true");
  el.setAttribute("spellcheck","false");
  el.focus();
  showBox(pickBox, el);
  showBlockBar(el);
}
function endEdit(){
  if(!editingEl) return;
  clearTimeout(_cmH);
  editingEl.removeAttribute("contenteditable");
  editingEl.removeAttribute("spellcheck");
  commit();
  editingEl = null;
  hideEl(textBar);
}


/* ---------- 块选中 ---------- */
function pickBlock(el){ endEdit(); closeAiPanel(); picked = el; showBox(pickBox, el); showBlockBar(el); }
function unpick(){ picked=null; hideEl(pickBox); hideEl(blockBar); }
function deselect(){ endEdit(); unpick(); hidePalette(); closeAiPanel(); hideEl(textBar); }



blockBar.addEventListener("click", e => {
  const act = e.target.closest("button")?.dataset.act;
  const el = picked || editingEl;
  if(!act || !el) return;
  if(act==="color"){ openPalette(el); return; }
  if(act==="ai"){ openAiPanel(el); return; }
  pushUndo();
  if(act==="dup"){ const c=el.cloneNode(true); c.removeAttribute("contenteditable"); el.after(c); toast("已复制一份到下方"); }
  if(act==="del"){ el.remove(); deselect(); toast("已删除"); }
  if(act==="up" && el.previousElementSibling) el.previousElementSibling.before(el);
  if(act==="down" && el.nextElementSibling) el.nextElementSibling.after(el);
  commit(); reposition();
});


/* 从当前权威 docHTML 中提取所有 <style> 块内文本并按出现顺序拼接（去重），供 AI 预览注入。
   reportHtml 的 <style> 随 docHTML 载入 #view iframe，因此这里提取到的内容与 #view 实际渲染
   使用的是同一份 CSS 语义，保证 AI 预览片段命中类名后与 #view 渲染完全一致。 */
function extractStyleFromHtml(html){
  if(!html) return "";
  const parts = [], seen = new Set();
  const re = /<style[^>]*>([\\s\\S]*?)<\\/style\\s*>/gi;
  let m;
  while((m = re.exec(html)) !== null){
    const text = (m[1] || "").trim();
    if(text && !seen.has(text)){ seen.add(text); parts.push(text); }
  }
  return parts.join("\\n");
}


/* ---------- AI 调试面板 ---------- */


let aiTarget = null;          // 当前 AI 调试选中的块
let aiBusy = false;           // 是否正在请求 AI 调试
let aiSaving = false;         // 是否正在提交“确认替换”保存中
let aiPendingHtml = "";       // 待用户确认的 AI 返回块 HTML
let aiPendingDeleted = false; // 是否删除块
const aiPreview = $("aiPreview"), aiPreviewBody = $("aiPreviewBody");
const aiApply = $("aiApply");


function openAiPanel(el){
  if(aiBusy) return;          // 请求中不允许切换目标
  aiTarget = el;
  aiInput.value = "";
  aiBar.classList.add("hidden");
  aiLoading.classList.remove("on");
  aiSend.disabled = false;
  aiInput.disabled = false;
  aiBar.textContent = "";
  resetAiPending();
  // 面板通过 CSS position:fixed + top:50%/left:50% + translate 实现浏览器窗口居中展示
  aiMask.style.display = "block";
  aiPanel.style.display = "flex";
  aiPanel.style.left = "";
  aiPanel.style.top = "";
  aiInput.focus();
}


function resetAiPending(){
  aiPendingHtml = ""; aiPendingDeleted = false;
  aiPreview.classList.remove("on");
  aiPreviewBody.innerHTML = "";
  aiApply.disabled = true;
}
function closeAiPanel(){
  aiMask.style.display = "none";
  aiPanel.style.display = "none";
  resetAiPending();
  if(!aiBusy){ aiLoading.classList.remove("on"); aiBar.classList.add("hidden"); }
}


$("aiClose").onclick = closeAiPanel;
$("aiPreviewClose").onclick = ()=>{ resetAiPending(); };
/* 回车发送，Shift+回车换行 */
aiInput.addEventListener("keydown", e=>{
  if(e.key==="Enter" && !e.shiftKey){ e.preventDefault(); sendAi(); }
});
aiSend.addEventListener("click", sendAi);
/* 支持再次点击发送：请求进行中或正在保存确认结果时暂不允许，其余情况可反复触发 */
function sendAi(){
  if(aiBusy){ toast("AI 仍在调试中，请稍候…"); return; }
  if(aiSaving){ toast("正在保存确认结果，请稍候…"); return; }
  const prompt = aiInput.value.trim();
  if(!prompt){ toast("请先输入要如何调整这块内容"); aiInput.focus(); return; }
  if(!aiTarget || !aiTarget.isConnected){ toast("目标块已不存在，请重新选择"); return; }
  const blockHtml = aiTarget.outerHTML;
  aiBusy = true;
  aiSend.disabled = true;
  aiBar.classList.add("hidden");
  aiInput.disabled = true;
  aiLoading.classList.add("on");
  resetAiPending();
  /* 编辑态无法直接点击，需记录目标与回调 */
  toast("已发给 AI 调试，请稍候…");
  setTimeout(()=>{ // 延迟极小避免 loading UI 未渲染即被替换
    window.parent && window.parent.postMessage({
      type: "SNAP_AI_REQUEST",
      payload: { blockHtml, prompt }
    }, "*");
  }, 20);
}
/**
 * 用户点击「确认替换」：此处仅把“应用 AI 结果后的字段内容”提交给父级保存，
 * 不会立刻替换真实文档。待父级保存接口成功返回（SNAP_AI_APPLIED）时才真正替换原文；
 * 若保存失败（SNAP_AI_SAVE_ERROR）则不替换原文，并保持可再次点击重试。
 */
aiApply.addEventListener("click", ()=>{
  if(aiSaving){ toast("正在保存确认结果，请稍候…"); return; }
  if(aiBusy){ toast("AI 仍在调试中，请稍候…"); return; }
  if(!aiTarget || !aiTarget.isConnected){ toast("目标块已不存在，请重新选择"); return; }
  if(!aiPendingHtml && !aiPendingDeleted){ toast("暂无可应用的结果"); return; }
  // 基于内存克隆所有 .js-view-edit 容器构造待保存内容（真实文档保持不变），
  // 仅在包含 aiTarget 的容器上应用 AI 结果，返回 { containers } 供父级更新字段。
  // 注意 .js-view-edit 容器可能不止一个，故按 DOM 顺序返回全部容器，保证索引与父级字段一致。
  const payload = buildApplyPayloadForSaving();
  if(!payload){
    toast("无法生成待保存的内容，请重新调试");
    return;
  }
  aiSaving = true;
  aiApply.disabled = true;
  aiBar.textContent = "正在保存，请稍候…";
  aiBar.classList.remove("hidden");
  window.parent && window.parent.postMessage(
    { type: "SNAP_AI_APPLY", payload },
    "*"
  );
  toast("AI 调试完成，正在保存…");
});
/**
 * 在内存中克隆所有 .js-view-edit 容器并应用 AI 结果，返回 { containers: [{ html }] }，
 * 不会触碰真实文档。仅对包含 aiTarget 的容器应用替换/删除，其余容器原样序列化，
 * 顺序与 DOM 中 .js-view-edit 出现顺序一致。
 */
function buildApplyPayloadForSaving(){
  if(!vdoc || !aiTarget) return null;
  const containers = vdoc.querySelectorAll(".js-view-edit");
  if(!containers.length) return null;
  const mark = "__snap_ai_apply_mark__";
  try { aiTarget.setAttribute("data-snap-mark", mark); } catch(e){ return null; }
  const items = [];
  for(const c of containers){
    const clone = c.cloneNode(true);
    let cloneTarget = null;
    try { cloneTarget = clone.querySelector('[data-snap-mark="' + mark + '"]'); } catch(e){}
    if(cloneTarget && cloneTarget.parentNode){
      if(aiPendingDeleted){
        cloneTarget.parentNode.removeChild(cloneTarget);
      } else if(aiPendingHtml){
        const frag = clone.ownerDocument.createRange().createContextualFragment(aiPendingHtml);
        const node = frag.firstElementChild;
        if(node){ cloneTarget.parentNode.replaceChild(node, cloneTarget); }
      }
    }
    items.push({ html: clone.innerHTML });
  }
  aiTarget.removeAttribute("data-snap-mark");
  return { containers: items };
}
/** 汇总当前所有 .js-view-edit 容器的内部 HTML 与字段名映射，回传父级 */
function buildFieldUpdatePayload(){
  if(!vdoc) return null;
  const containers = vdoc.querySelectorAll(".js-view-edit");
  if(!containers.length) return null;
  const items = [];
  for(const c of containers){
    items.push({ html: c.innerHTML });
  }
  return { containers: items };
}
/* 与父组件双向通信：接收 AI 结果 */
window.addEventListener("message", e=>{
  const d = e.data || {};
  if(d.type === "SNAP_EDITOR_GET_HTML_REQUEST"){
    // 父级下载时请求拉取编辑器当前最新内容。一次性计算返回，不维护持久状态
    //（取数与 #btnSave 导出一致：脚本开关打开时用缓存权威 docHTML，否则序列化 #view DOM）。
    try{
      const out = $("tglScript").checked ? docHTML : serialize();
      window.parent.postMessage({ type: "SNAP_EDITOR_GET_HTML_RESULT", html: out, requestId: d.requestId }, "*");
    }catch(err){
      window.parent.postMessage({ type: "SNAP_EDITOR_GET_HTML_RESULT", html: "", requestId: d.requestId }, "*");
    }
    return;
  }
  if(d.type === "SNAP_AI_RESULT"){
    aiBusy = false; aiSend.disabled = false; aiInput.disabled = false; aiLoading.classList.remove("on");
    const html = d.payload?.html || d.html;
    const deleted = !!(d.payload && d.payload.deleted);
    if(html){
      aiPendingHtml = html; aiPendingDeleted = false;
      aiPreviewBody.innerHTML = "";
      // 用专用包裹容器隔离 AI 预览，注入 reportHtml 的 <style>（复用初始化时一次性提取的 reportStyleCache），
      // 使预览片段能命中 reportHtml 定义的类名，且与 #view 渲染完全一致；同时避免污染外壳 UI。
      const scope = document.createElement("div");
      scope.className = "snap-ai-preview-scope";
      const reportCss = reportStyleCache;
      if(reportCss){
        const sty = document.createElement("style");
        sty.textContent = reportCss;
        scope.appendChild(sty);
      }
      const previewEl = vdoc.createElement("div");
      previewEl.innerHTML = html;
      while(previewEl.firstChild) scope.appendChild(previewEl.firstChild);
      aiPreviewBody.appendChild(scope);
      aiPreview.classList.add("on");
      aiApply.disabled = false;
      aiBar.textContent = "AI 已生成新版本，请预览后确认替换。";
      aiBar.classList.remove("hidden");
    } else if(deleted){
      aiPendingHtml = ""; aiPendingDeleted = true;
      aiPreviewBody.innerHTML = "<em style='color:#B91C1C'>AI 将删除当前选中的块。</em>";
      aiPreview.classList.add("on");
      aiApply.disabled = false;
      aiBar.textContent = "AI 已删除该块，请预览后确认替换。";
      aiBar.classList.remove("hidden");
    } else {
      resetAiPending();
      aiBar.textContent = "✗ AI 未返回有效内容";
      aiBar.classList.remove("hidden");
    }
  } else if(d.type === "SNAP_AI_APPLIED"){
    // 保存接口已成功返回：此刻才真正将 AI 结果应用到文档（替换原文）
    aiBusy = false; aiSend.disabled = false; aiInput.disabled = false; aiLoading.classList.remove("on");
    aiSaving = false;
    if(aiPendingHtml || aiPendingDeleted){
      if(aiTarget && aiTarget.isConnected){
        try{
          if(aiPendingDeleted){
            const prev = aiTarget.previousElementSibling || aiTarget.nextElementSibling;
            pushUndo();
            aiTarget.remove();
            aiTarget = null; editingEl = null; picked = prev || null;
            commit(); if(picked) reposition();
          } else if(aiPendingHtml){
            const node = vdoc.createRange().createContextualFragment(aiPendingHtml).firstElementChild;
            if(node){
              pushUndo();
              aiTarget.replaceWith(node);
              picked = node; editingEl = null;
              commit(); reposition();
            }
          }
        }catch(err){
          toast("应用 AI 结果失败: " + err.message);
        }
      }
    }
    closeAiPanel();
    deselect();
    toast("保存成功，AI 调试结果已应用到报告");
  } else if(d.type === "SNAP_AI_SAVE_ERROR"){
    // 保存接口返回失败：不替换原文，保留待确认结果，并允许再次点击“确认替换”重试
    aiBusy = false; aiSend.disabled = false; aiInput.disabled = false; aiLoading.classList.remove("on");
    aiSaving = false;
    aiApply.disabled = false;
    aiBar.textContent = "✗ 保存失败: " + (d.message || d.payload?.message || "未知错误") + "。未替换原文，可再次点击确认替换重试。";
    aiBar.classList.remove("hidden");
    toast("保存失败，未替换原文，可再次点击确认替换");
  } else if(d.type === "SNAP_AI_ERROR"){


    aiBusy = false; aiSend.disabled = false; aiInput.disabled = false; aiLoading.classList.remove("on");
    aiSaving = false; aiApply.disabled = false;
    resetAiPending();
    aiBar.textContent = "✗ AI 调试失败: " + (d.message || d.payload?.message || "未知错误");
    aiBar.classList.remove("hidden");
    toast("AI 调试失败");
  }
});




/* ---------- 选中文字样式 ---------- */


function onSelChange(){
  if(!editingEl){ hideEl(textBar); return; }
  const sel = vdoc.getSelection();
  if(!sel || sel.isCollapsed || !sel.rangeCount){ hideEl(textBar); return; }
  const r = sel.getRangeAt(0).getBoundingClientRect();
  if(!r.width && !r.height){ hideEl(textBar); return; }
  const f = view.getBoundingClientRect();
  textBar.style.display="flex";
  const w = textBar.offsetWidth || 240;
  let x = f.left + r.left + r.width/2 - w/2;
  x = Math.max(8, Math.min(x, window.innerWidth - w - 8));
  let y = f.top + r.top - 42;
  if(y < 52) y = f.top + r.bottom + 8;
  textBar.style.left = x+"px"; textBar.style.top = y+"px";
}
textBar.addEventListener("mousedown", e=> e.preventDefault()); // 保住选区
textBar.addEventListener("click", e => {
  const btn = e.target.closest("button");
  if(!btn || !vdoc) return;
  if(btn.dataset.cmd){ pushUndo(); vdoc.execCommand(btn.dataset.cmd); commit(); return; }
  if(btn.dataset.fs){ stepSelFontSize(+btn.dataset.fs); }
});
/* 选区字号微调：包一层 span 改 font-size */
function stepSelFontSize(dir){
  const sel = vdoc.getSelection();
  if(!sel || sel.isCollapsed || !sel.rangeCount) return;
  const anchor = sel.anchorNode.nodeType===3 ? sel.anchorNode.parentElement : sel.anchorNode;
  const cur = parseFloat(view.contentWindow.getComputedStyle(anchor).fontSize) || 14;
  const next = Math.max(9, Math.min(72, cur + dir*1));
  pushUndo();
  vdoc.execCommand("fontSize", false, 7);                    // 占位
  vdoc.querySelectorAll('font[size="7"]').forEach(f=>{
    const s = vdoc.createElement("span");
    s.style.fontSize = next+"px";
    while(f.firstChild) s.appendChild(f.firstChild);
    f.replaceWith(s);
  });
  commit();
}
let selUndoDone = false;
function selCmd(cmd, val){
  if(!vdoc) return;
  if(!selUndoDone){ pushUndo(); selUndoDone = true; }
  vdoc.execCommand(cmd,false,val); commitSoon();
}
$("selFore").addEventListener("pointerdown", ()=> selUndoDone=false);
$("selBack").addEventListener("pointerdown", ()=> selUndoDone=false);
$("selFore").addEventListener("input", e=> selCmd("foreColor", e.target.value));
$("selBack").addEventListener("input", e=> selCmd("hiliteColor", e.target.value));


/* ---------- 颜色面板 ---------- */
let palTarget = null;
let lastProp = "backgroundColor";
let palUndone = {};
const PROPS = [["pText","pTextHex","color"],["pBg","pBgHex","backgroundColor"],["pBd","pBdHex","borderColor"]];
function toHex(c){
  const m = c && c.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
  if(!m) return "";
  return "#"+[m[1],m[2],m[3]].map(n=>(+n).toString(16).padStart(2,"0")).join("").toUpperCase();
}
function openPalette(el){
  palTarget = el;
  palUndone = {};
  lastProp = "backgroundColor";
  const cs = view.contentWindow.getComputedStyle(el);
  PROPS.forEach(([pid,hid,prop])=>{
    const hex = toHex(cs[prop]) || "#000000";
    $(pid).value = hex; $(hid).value = hex;
  });
  palette.style.display = "block";
  const bar = blockBar.getBoundingClientRect();
  let x = Math.min(bar.left, window.innerWidth-296);
  let y = bar.bottom + 8;
  if(y + 240 > window.innerHeight) y = Math.max(8, bar.top - 248);
  palette.style.left = Math.max(8,x)+"px"; palette.style.top = y+"px";
}
function hidePalette(){ palette.style.display="none"; palTarget=null; }
$("palClose").onclick = hidePalette;
PROPS.forEach(([pid,hid,prop])=>{
  $(pid).addEventListener("pointerdown", ()=> lastProp=prop);
  $(hid).addEventListener("focus", ()=> lastProp=prop);
  $(pid).addEventListener("input", e=>{ applyColor(prop, e.target.value); $(hid).value=e.target.value; });
  $(hid).addEventListener("change", e=>{
    let v = e.target.value.trim();
    if(!/^#?[0-9a-fA-F]{6}$/.test(v)){ toast("请输入 6 位色值，如 #EA580C"); return; }
    if(v[0]!=="#") v = "#"+v;
    $(pid).value=v; applyColor(prop, v);
  });
});
palette.addEventListener("click", e=>{
  const p = e.target.closest(".clr")?.dataset.p;
  if(p && palTarget){ pushUndo(); palTarget.style[p]=""; commit(); toast("已清除该项颜色"); return; }
  const efs = e.target.closest("[data-efs]")?.dataset.efs;
  if(efs && palTarget){
    const cur = parseFloat(view.contentWindow.getComputedStyle(palTarget).fontSize) || 14;
    pushUndo(); palTarget.style.fontSize = Math.max(9, Math.min(72, cur + (+efs))) + "px"; commit();
  }
});
$("pBold").onclick = ()=>{
  if(!palTarget) return;
  const cs = view.contentWindow.getComputedStyle(palTarget);
  const bold = parseInt(cs.fontWeight,10) >= 600;
  pushUndo(); palTarget.style.fontWeight = bold ? "normal" : "bold"; commit();
};
function applyColor(prop, val){
  if(!palTarget) return;
  if(!palUndone[prop]){ pushUndo(); palUndone[prop]=true; }
  palTarget.style[prop]=val; commitSoon();
}
const PROP2ID = {color:["pText","pTextHex"], backgroundColor:["pBg","pBgHex"], borderColor:["pBd","pBdHex"]};
const swBox = $("swatches");
SWATCH.forEach(c=>{
  const s=document.createElement("span"); s.className="sw"; s.style.background=c; s.title=c;
  s.onclick=()=>{ applyColor(lastProp, c); const [pid,hid]=PROP2ID[lastProp]; $(pid).value=c; $(hid).value=c; };
  swBox.appendChild(s);
});


/* ---------- 覆盖层定位 ---------- */
function showBox(box, el){
  const r = el.getBoundingClientRect(), f = view.getBoundingClientRect();
  const top = Math.max(f.top, f.top + r.top), left = f.left + r.left;
  const bottom = Math.min(f.bottom, f.top + r.bottom);
  if(bottom <= f.top || (f.top + r.top) >= f.bottom){ box.style.display="none"; return; }
  box.style.display="block";
  box.style.left=left+"px"; box.style.top=top+"px";
  box.style.width=r.width+"px"; box.style.height=(bottom-top)+"px";
}
function hideEl(el){ el.style.display="none"; }
// 是否为最外层容器：直接挂载在 body 下的顶层块
function isOuterContainer(el){ return !!(vdoc && el.parentNode === vdoc.body); }
function showBlockBar(el){
  const r = el.getBoundingClientRect(), f = view.getBoundingClientRect();
  blockBar.style.display="flex";
  const bw = blockBar.offsetWidth || 300;
  const bh = blockBar.offsetHeight || 38;
  const outer = isOuterContainer(el);
  const contentLeft = f.left + r.left;
  const contentTop = f.top + r.top;
  let x, y;
  if(outer){
    // 最外层容器：展示在容器内部左上角，避免溢出 / 遮挡顶部
    x = Math.max(f.left, Math.min(contentLeft + 8, contentLeft + r.width - bw - 24));
    y = Math.max(f.top, Math.min(contentTop + 8, contentTop + r.height - bh - 24));
  } else {
    // 普通元素：展示在选中元素左上角（贴元素上沿、左对齐）
    x = Math.max(8, Math.min(contentLeft, window.innerWidth - bw - 8));
    y = contentTop - bh - 4;
    if(y < f.top + 4){ // 上方空间不足，转至元素内部左上角
      y = Math.min(window.innerHeight - bh - 8, contentTop + 8);
    }
  }
  blockBar.style.left=x+"px"; blockBar.style.top=y+"px";
}
function reposition(){
  hideEl(hoverBox);
  const el = picked || editingEl;
  if(el && el.isConnected){ showBox(pickBox, el); showBlockBar(el); }
  else { hideEl(pickBox); hideEl(blockBar); }
  hidePalette(); hideEl(textBar);
}


function clearUI(){ picked=null; editingEl=null; [hoverBox,pickBox,blockBar,textBar].forEach(hideEl); hidePalette(); closeAiPanel(); }



/* ---------- 顶栏动作 ---------- */
$("tglEdit").addEventListener("change", ()=>{ deselect(); updateTip(); });
$("tglScript").addEventListener("change", e=>{
  if(e.target.checked && !e.target._warned){
    e.target._warned = true;
    if(!confirm("动态预览会执行文件里的 JavaScript，只建议对自己生成或可信的文件开启。继续？")){ e.target.checked=false; return; }
  }
  $("tglEdit").disabled = e.target.checked;
  deselect(); render();
});
$("btnUndo").onclick = ()=>{
  if(!undoStack.length) return;
  docHTML = undoStack.pop();
  $("btnUndo").disabled = !undoStack.length;
  setDirty(docHTML !== rawOriginal);
  render(); toast("已撤销");
};
$("btnReset").onclick = ()=>{
  if(!confirm("放弃所有修改，回到刚打开时的样子？")) return;
  docHTML = rawOriginal; undoStack=[]; $("btnUndo").disabled=true;
  setDirty(false); render(); toast("已还原");
};
/* 导出前尽量把图片转成内嵌 data URL，换电脑打开不掉图 */
async function inlineImages(){
  if(!vdoc) return;
  const imgs = [...vdoc.querySelectorAll("img")].filter(i => i.src && !i.src.startsWith("data:") && i.complete && i.naturalWidth > 0);
  let done = 0;
  for(const img of imgs){
    try{
      const c = document.createElement("canvas");
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      c.getContext("2d").drawImage(img, 0, 0);
      img.src = c.toDataURL("image/png");
      done++;
    }catch(e){ /* 跨域受限的图保留原路径 */ }
  }
  if(done) toast("已内嵌 " + done + " 张图片");
}
$("btnSave").onclick = async ()=>{
  endEdit();
  if(!$("tglScript").checked) await inlineImages();
  const out = $("tglScript").checked ? docHTML : serialize();
  const blob = new Blob([out], {type:"text/html;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "report-改.html";
  a.click(); URL.revokeObjectURL(a.href);
  toast("已导出：" + a.download);
};
document.addEventListener("keydown", e=>{
  if((e.metaKey||e.ctrlKey) && e.key==="s"){ e.preventDefault(); if(!$("btnSave").disabled) $("btnSave").click(); }
  if(e.key==="Escape") deselect();
});
document.addEventListener("mousedown", e=>{
  // 点击蒙层不关闭 AI 调试弹窗：蒙层仅作视觉遮罩，点击它保持弹窗与选中态不变
  if(aiMask.contains(e.target)){ e.preventDefault(); return; }
  if(!palette.contains(e.target) && !blockBar.contains(e.target) && !textBar.contains(e.target) && !aiPanel.contains(e.target) && e.target!==view) {
    if(!e.target.closest(".bar")) deselect();
  }
});



window.addEventListener("beforeunload", e=>{ if(dirty){ e.preventDefault(); e.returnValue=""; } });


/* ---------- 初始化：自动载入报告 ---------- */
(function init(){
  reportStyleCache = extractStyleFromHtml(rawOriginal);   // 仅初始化时提取一次 reportHtml 的 <style>
  ["tglEdit","tglScript","btnReset","btnSave"].forEach(id=>$(id).disabled=false);
  $("tglEdit").checked = true;
  $("tglScript").checked = false;
  setDirty(false);
  render();
  toast("报告已载入，可直接编辑");
})();
})();
`;


/**
 * 将报告 HTML 转义为可安全嵌入 <script> 双引号字符串的字面量。
 * 依次转义反斜杠、双引号、换行，并关闭可能提前拖死外层 <script> 的 </script>。
 */
function escapeForScriptLiteral(input: string): string {
  return input
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\r/g, "\\r")
    .replace(/\n/g, "\\n")
    .replace(/<\/script>/gi, "<\\/script>");
}


/**
 * 生成「随手改」HTML 可视化编辑器页面（可直接作为 iframe srcdoc 注入）。
 *
 * @param reportHtml - 待编辑的报告 HTML 内容（作为编辑器初始文档载入）
 * @param title - 可选，编辑器顶栏展示的文件名/标题
 * @returns 完整编辑器 HTML 字符串
 */
export function buildSnapEditorHtml(
  reportHtml: string,
  title = "报告编辑",
): string {
  const safeReport = escapeForScriptLiteral(reportHtml || "");


  const script = SNAP_EDITOR_SCRIPT.replace("__INITIAL_HTML__", safeReport);



  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
${SNAP_EDITOR_STYLE}
</style>
</head>
<body>
${buildShellBody()}
<script>
${script}
<\/script>
</body>
</html>`;
}