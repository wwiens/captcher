// Runs inside the isolated Chrome smoke harness with synthetic values only.
const assert = require('node:assert/strict');
module.exports = async ({cdp,workerSession,panelSession,tab,origin}) => {
  const page = async (body) => (await cdp.eval(workerSession,
    `chrome.scripting.executeScript({target:{tabId:${tab.id}},func:async()=>{${body}}})`))[0].result;
  // shotLog: the panel's last screenshot line, so a missing screenshot fails with its reason.
  const capture = () => cdp.eval(panelSession, `(async()=>{const shotLog=()=>(el.log.textContent.match(/screenshot (skipped|discarded)[^\\n]*?(?=\\d\\d:\\d\\d:\\d\\d|$)/g)||[]).pop()||'';try{const c=await performCapture(await chrome.tabs.get(${tab.id}));return {content:c.content,screenshot:!!c.screenshot,shotLog:shotLog()};}catch(e){return {error:e.message,shotLog:shotLog()};}})()`);
  const reset = () => page('document.body.innerHTML="<h1>Synthetic test</h1><input id=field>";');
  // Dashboard pages include inline SVG icons and controls inside closed dialogs.
  await reset();
  await page(`document.body.insertAdjacentHTML('beforeend', '<svg viewBox="0 0 10 10"><path d="M0 0h10v10H0z"/></svg><div hidden><input type=search><select><option>Newest</option></select></div><dialog><input type=number min=240 max=2000><textarea readonly></textarea></dialog>');`);
  const dashboard = await capture();
  assert.ok(!dashboard.error, 'dashboard controls and SVG: '+dashboard.error);
  assert.ok(dashboard.content.includes('<svg'), 'inline SVG survives capture');
  // Ordinary hidden inputs ("page=1", "remember=true") are blanked but never
  // hunted through the page: a short one used to abort every capture of the
  // page, and a longer one starred out unrelated text. Token-like hidden
  // values and fields named like secrets are still removed everywhere.
  await reset();
  await page(`document.body.insertAdjacentHTML('beforeend', '<p>Page 1 of 3</p><p>true story</p><input id=h1 type=hidden name=page value=1><input id=h2 type=hidden name=remember value=true><input id=h3 type=hidden name=csrf value=SCHiddenToken0123456789><p>SCHiddenToken0123456789</p><input id=pin name=cardPin value=SCPin4321><select autocomplete=cc-exp-month><option>01</option><option selected>09</option></select><p>Due 2026-09-01</p>');`);
  const ordinary = await capture();
  assert.ok(!ordinary.error, 'ordinary hidden inputs: '+ordinary.error);
  for (const text of ['Page 1 of 3','true story','Due 2026-09-01']) assert.ok(ordinary.content.includes(text), 'HTML keeps '+text);
  for (const secret of ['SCHiddenToken','SCPin4321']) assert.ok(!ordinary.content.includes(secret), 'HTML contains '+secret);
  assert.deepEqual(await page(`return [...document.querySelectorAll('#h1,#h2,#h3,#pin')].map(el=>el.value);`),
    ['1','true','SCHiddenToken0123456789','SCPin4321'],'hidden and named-secret values restored');
  await reset();
  await page(`
    document.body.insertAdjacentHTML('beforeend', '<template id=tpl><input autocomplete=one-time-code value=SCTemplateSecret123></template><input id=p type=password value="SCPass&amp;123"><input id=h type=hidden value="SCHiddenDefault123"><textarea id=t autocomplete=one-time-code>SCDefaultTextarea123</textarea><input id=c autocomplete=cc-csc value=937><select id=s autocomplete=cc-name><option value=SCOptionOne>SCFirstName</option><option value=SCOptionTwo selected>SCSecondName</option></select>');
    document.querySelector('#h').value='SCHiddenLive123';
    document.querySelector('#t').value='SCLiveTextarea123';
    for(const mode of ['open','closed']) {
      const host=document.createElement('div');document.body.append(host);
      host.attachShadow({mode}).innerHTML='<input autocomplete=one-time-code value=SCShadow'+mode+'123>';
    }
    document.body.insertAdjacentHTML('beforeend', '<svg><foreignObject><div xmlns="http://www.w3.org/1999/xhtml" id="foreign-host"></div></foreignObject></svg><math><mi>x</mi></math>');
    document.querySelector('#foreign-host').attachShadow({mode:'closed'}).innerHTML='<input type=password value=SCForeignSecret123>';
    const frame=document.createElement('iframe');frame.srcdoc='<svg><circle r="4"/></svg><input autocomplete=cc-number value="SCFrame&amp;123"><p>SCFrame&amp;123</p>';
    document.body.append(frame);await new Promise(resolve=>frame.onload=resolve);
  `);
  // The screenshot is always taken now, so what matters is that every
  // sensitive control is already blank when it is: the shot sees the page as
  // the DOM pass left it. Observed from inside the real capture path.
  await cdp.eval(panelSession, `(()=>{const original=takeViewportScreenshot;self.__shotOriginal=original;takeViewportScreenshot=async function(tab){
    const [r]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>[...document.querySelectorAll('#p,#h,#t,#c')].map(el=>el.value)});
    self.__atShot=r.result;return original(tab);};})()`);
  const first = await capture();
  const atShot = await cdp.eval(panelSession, `(()=>{takeViewportScreenshot=self.__shotOriginal;return self.__atShot;})()`);
  assert.ok(!first.error, first.error);
  assert.equal(first.screenshot,true,'a screenshot is taken despite sensitive controls and frames');
  assert.deepEqual(atShot,['','','',''],'sensitive controls are blank when the screenshot is taken');
  for (const secret of ['SCPass','SCHidden','SCDefaultTextarea','SCLiveTextarea','SCOption','SCFirstName','SCSecondName','SCShadow','SCFrame','SCForeignSecret','SCTemplate','937']) {
    assert.ok(!first.content.includes(secret), 'HTML contains '+secret);
  }
  assert.deepEqual(await page(`return [...document.querySelectorAll('#p,#h,#t,#c,#s')].map(el=>el.value);`),
    ['SCPass&123','SCHiddenLive123','SCLiveTextarea123','937','SCOptionTwo'],'live values restored including selected option');
  assert.equal(await page(`return document.querySelector('#tpl').content.querySelector('input').value;`),'SCTemplateSecret123');
  assert.equal(await page(`return document.querySelector('#t').textContent;`),'SCDefaultTextarea123');
  assert.equal(await page(`return chrome.dom.openOrClosedShadowRoot(document.querySelectorAll('div')[1]).querySelector('input').value;`),'SCShadowclosed123');
  assert.equal(await page(`return chrome.dom.openOrClosedShadowRoot(document.querySelector('#foreign-host')).querySelector('input').value;`),'SCForeignSecret123');

  await reset();
  const rollback = await page(`
    document.body.insertAdjacentHTML('beforeend','<input id=first type=password value=SCDefault123><input id=bad type=password>');
    document.querySelector('#first').value='SCLive123';
    Object.defineProperty(document.querySelector('#bad'),'value',{get:()=> 'SCBad123',set:()=>{throw new Error('Synthetic setter failure');}});
    let refused=false;try{scSensitive.redactAll('rollback');}catch{refused=true;}
    return {refused,value:document.querySelector('#first').value,defaultValue:document.querySelector('#first').defaultValue};
  `);
  assert.deepEqual(rollback,{refused:true,value:'SCLive123',defaultValue:'SCDefault123'},'partial blanking failure restores earlier fields');
  await reset();
  const expired = await page(`
    document.querySelector('#field').type='password';document.querySelector('#field').value='SCExpiry123';
    const original=setTimeout;
    try {self.setTimeout=(fn,ms)=>original(fn,ms===180000?1:ms);scSensitive.redactAll('expiry');}
    finally{self.setTimeout=original;}
    await new Promise(resolve=>original(resolve,20));
    let refused=false;try{scSensitive.verify('expiry');}catch{refused=true;}
    return {refused,value:document.querySelector('#field').value};
  `);
  assert.deepEqual(expired,{refused:true,value:'SCExpiry123'},'owner loss fallback restores fields and invalidates further output');
  // Direct guard exercises cover transaction behavior independently of SingleFile.
  await reset();
  const encoded = await page(`
    document.querySelector('#field').outerHTML='<input id=field autocomplete=one-time-code value="SCSecret&amp;123">';
    scSensitive.redactAll('encoding');
    try {
      const raw='SCSecret&123', entity='SCSecret&amp;123';
      const html='<p>'+entity+'</p><iframe src="data:text/html;base64,'+btoa('<p>'+entity+'</p>')+'"></iframe>';
      const result=scSensitive.scrub(html,'encoding');
      const nested=atob(result.html.match(/base64,([^\"]+)/)[1]);
      return {html:result.html,nested};
    } finally { scSensitive.restoreAll('encoding'); }
  `);
  assert.ok(!encoded.html.includes('SCSecret') && !encoded.nested.includes('SCSecret'));
  const owner = await page(`
    scSensitive.redactAll('owner');let refused=false;
    try { scSensitive.redactAll('other'); } catch { refused=true; }
    const restored=scSensitive.restoreAll('other');
    const blank=document.querySelector('#field').value;
    scSensitive.restoreAll('owner');return {refused,restored,blank,value:document.querySelector('#field').value};
  `);
  assert.deepEqual(owner,{refused:true,restored:0,blank:'',value:'SCSecret&123'});
  const refill = await page(`
    scSensitive.redactAll('refill');document.querySelector('#field').value='SCNewApplicationValue';let refused=false;
    try { scSensitive.scrub('<p>safe</p>','refill'); } catch { refused=true; }
    scSensitive.restoreAll('refill');return {refused,value:document.querySelector('#field').value};
  `);
  assert.deepEqual(refill,{refused:true,value:'SCNewApplicationValue'},'refill rejects and cleanup preserves newer value');
  const short = await page(`
    document.querySelector('#field').value='937';scSensitive.redactAll('short');
    try { scSensitive.scrub('<p>937</p>','short');return false; } catch {return true;}
    finally {scSensitive.restoreAll('short');}
  `);
  assert.equal(short,true,'ambiguous short reflections abort');
  const changed = await page(`
    scSensitive.redactAll('changed');const replacement=document.querySelector('#field').cloneNode();replacement.value='SCReplacement123';document.querySelector('#field').replaceWith(replacement);
    try { scSensitive.verify('changed');return false;}catch{return true;}finally{scSensitive.restoreAll('changed');}
  `);
  assert.equal(changed,true,'replacement controls invalidate protection');
  // 1Password mounts its field button and menu (closed shadow roots, an
  // extension-page frame) as fields take and lose focus. That churn must not
  // abort a capture; the page's own new shadow root still does, and a
  // control inside foreign UI is still blanked.
  await reset();
  const passwordManager = await page(`
    const mount=()=>{const host=document.createElement('com-1password-button');
      host.attachShadow({mode:'closed'}).innerHTML='<span>1P</span><input type=password value=SCForeignUi123>';
      const live=document.createElement('div');live.id='1p-menu-live-region';
      const frame=document.createElement('iframe');frame.src='chrome-extension://aeblfdkhhhdcdjpifhhbdiojplfjncoa/inline/menu/menu.html';
      document.body.append(host,live,frame);return [host,live,frame];};
    const first=mount();await new Promise(resolve=>setTimeout(resolve,50));
    scSensitive.redactAll('foreign');
    const blank=chrome.dom.openOrClosedShadowRoot(first[0]).querySelector('input').value;
    let churned=true;
    try {
      first[2].remove();first[1].remove();
      const second=mount();second[0].remove();
      await new Promise(resolve=>setTimeout(resolve,50));
      scSensitive.verify('foreign');
    } catch { churned=false; } finally { scSensitive.restoreAll('foreign'); }
    scSensitive.redactAll('own');let own=false;
    try { const host=document.createElement('div');host.attachShadow({mode:'closed'});document.body.append(host);scSensitive.verify('own'); }
    catch { own=true; } finally { scSensitive.restoreAll('own'); }
    return {blank,churned,own,value:chrome.dom.openOrClosedShadowRoot(first[0]).querySelector('input').value};
  `);
  assert.deepEqual(passwordManager,{blank:'',churned:true,own:true,value:'SCForeignUi123'},'password-manager UI churn is tolerated, page roots are not');
  // Removal is safe and tolerated; an addition still aborts, and the failure
  // names the custom element that appeared, also when the observer saw it first.
  await reset();
  const churn = await page(`
    const widget=(tag)=>{const host=document.createElement(tag);host.attachShadow({mode:'closed'}).innerHTML='<b>x</b>';return host;};
    const gone=widget('acme-overlay');document.body.append(gone);
    scSensitive.redactAll('removal');let removed;
    try { gone.remove();await new Promise(resolve=>setTimeout(resolve));scSensitive.verify('removal');removed=true; }
    catch { removed=false; } finally { scSensitive.restoreAll('removal'); }
    scSensitive.redactAll('addition');let added;
    try { const outer=widget('acme-toolbar');document.body.append(outer);
      chrome.dom.openOrClosedShadowRoot(outer).append(widget('acme-button'));
      await new Promise(resolve=>setTimeout(resolve));scSensitive.verify('addition');added='passed'; }
    catch(e) { added=e.message; } finally { scSensitive.restoreAll('addition'); }
    return {removed,added};
  `);
  assert.equal(churn.removed,true,'removed page content does not abort');
  assert.match(churn.added,/could not be verified\. <acme-toolbar> was added .*Chrome profile without other extensions/,'addition aborts and names the outermost element');
  await reset();
  await page(`const frame=document.createElement('iframe');frame.sandbox='';frame.srcdoc='<input value=SCInaccessible123>';document.body.append(frame);await new Promise(resolve=>frame.onload=resolve);`);
  const blocked = await capture();assert.match(blocked.error,/embedded frame cannot be inspected/);
  // Cross-origin frames (Google Forms, embeds, widgets) are opaque to the
  // engine too: capture proceeds and the frame stays empty in the HTML. The
  // screenshot shows it as it stands — nothing can blank its contents.
  await reset();
  await page(`const frame=document.createElement('iframe');frame.src=${JSON.stringify(origin.replace('127.0.0.1','localhost')+'/recorded')};document.body.append(frame);await new Promise(resolve=>frame.onload=resolve);`);
  const opaque = await capture();
  assert.ok(!opaque.error, 'cross-origin frame: '+opaque.error);
  assert.equal(opaque.screenshot,true,'a screenshot is taken with a cross-origin frame: '+opaque.shotLog);
  // App chrome (HubSpot's nav) parks 0x0, display:none and off-screen frames on
  // every page, and forms carry hidden inputs.
  await reset();
  await page(`const src=${JSON.stringify(origin.replace('127.0.0.1','localhost')+'/recorded')};
    const styles=['width:0;height:0;border:0','display:none','position:fixed;left:-5000px;top:0'];
    await Promise.all(styles.map(style=>{const frame=document.createElement('iframe');frame.src=src;frame.style.cssText=style;document.body.append(frame);return new Promise(resolve=>frame.onload=resolve);}));
    document.body.insertAdjacentHTML('beforeend','<input type=hidden name=page value=1>');`);
  const unpainted = await capture();
  assert.ok(!unpainted.error, 'unpainted frames: '+unpainted.error);
  assert.equal(unpainted.screenshot,true,'unpainted frames and hidden inputs keep the screenshot');
  // Scroll offsets survive serialization as data-sc-scroll stamps, and the
  // live page is left exactly as it was.
  await reset();
  await page(`document.body.insertAdjacentHTML('beforeend','<div id=sbox style="height:100px;overflow:auto"><div style="height:1000px">tall</div></div>');document.querySelector('#sbox').scrollTop=120;`);
  const scrolled = await capture();
  assert.ok(!scrolled.error, 'scrolled box: '+scrolled.error);
  assert.match(scrolled.content, /id=["']?sbox["']?[^>]*data-sc-scroll=["']?120 0|data-sc-scroll=["']?120 0["']?[^>]*id=["']?sbox/, 'scroll offset stamped into the snapshot');
  assert.deepEqual(await page(`const e=document.querySelector('#sbox');return [e.hasAttribute('data-sc-scroll'),e.scrollTop];`),[false,120],'live page unstamped and still scrolled');
  assert.equal(await page(`return scSensitive.restoreAll('unknown');`),0);
  await reset();
  // Real capture refills a control immediately before engine execution.
  await page(`document.querySelector('#field').setAttribute('autocomplete','one-time-code');document.querySelector('#field').value='SCBeforeRefill123';`);
  const rejected = await cdp.eval(panelSession, `(async()=>{const original=runCaptureInPage;try{runCaptureInPage=async function(options,lab,id){document.querySelector('#field').value='SCRefilled123';try{self.scSensitive.verify(id);}catch(e){return {error:e.message};}};try{await performCapture(await chrome.tabs.get(${tab.id}));return false;}catch(e){return e.message;}}finally{runCaptureInPage=original;}})()`);
  assert.match(rejected,/protection could not be verified/,'capture does not produce output when redaction verification throws');
  await reset();
  console.log('PASS: redaction covers closed roots, same-origin frames, hidden values, textarea defaults, dropdowns, short CSCs and encoded reflections; screenshots are taken after redaction; scroll offsets are stamped; unsafe frames/refills/replacements abort; cleanup preserves form state and transaction ownership.');
};
