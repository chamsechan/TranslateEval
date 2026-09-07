const {chromium} = require('__PLAYWRIGHT_MODULE__');
const fs=require('fs');
(async()=>{
 const browser=await chromium.launch({executablePath:'__CHROMIUM_EXE__',headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1080}});
 const errors=[]; const validated=[];page.on('pageerror',e=>errors.push(e.message));page.on('response',async r=>{if(/import-reports.*validate/.test(r.url()))validated.push((await r.json()).id)});
 await page.goto('__API_BASE__/#/submit');
 await page.getByLabel('服务器目录、ZIP 或 JSONL 路径').fill('__REVIEW_ROOT__/bad-results');
 await page.getByRole('button',{name:'读取并填写导入信息',exact:true}).click();
 await page.getByRole('button',{name:'核验模型信息、数据集版本和预测 ID',exact:true}).waitFor();
 await page.screenshot({path:'__REVIEW_ROOT__/bad-manifest.png',fullPage:true});
 const combos=await page.locator('input[role="combobox"]').evaluateAll(nodes=>nodes.map(n=>({id:n.id,disabled:n.disabled,value:n.value})));
 fs.writeFileSync('__REVIEW_ROOT__/evidence-bad-manifest.json',JSON.stringify({comboboxes:combos,body:await page.locator('body').innerText(),pageErrors:errors},null,2));
 console.log(JSON.stringify({badManifestDatasetComboboxes:combos.filter(c=>/datasets/.test(c.id))}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
