const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createSidebar} = require('../content/content.js');
function fixture() {
  const classes=new Set(['existing']);
  const make=tag=>({tag,children:[],isConnected:false,appendChild(child){this.children.push(child);child.parent=this;return child;},
    prepend(child){this.children.unshift(child);child.parent=this;child.isConnected=true;},
    remove(){this.isConnected=false;this.parent.children=this.parent.children.filter(c=>c!==this);}});
  const body=make('body');body.classList={add:c=>classes.add(c),remove:c=>classes.delete(c)};
  const doc={body,createElement:make};const runtime={getURL:p=>'chrome-extension://kurukin/'+p};
  let closed=0;const sidebar=createSidebar(doc,runtime,()=>closed++);
  return {sidebar,doc,classes,get closed(){return closed;}};
}
test('sidebar opens',()=>{const f=fixture();f.sidebar.open();assert.equal(f.doc.body.children[0].id,'kurukin-container');assert.ok(f.classes.has('kurukin-panel-open'));});
test('sidebar closes',()=>{const f=fixture();f.sidebar.open();f.sidebar.close();assert.equal(f.doc.body.children.length,0);assert.equal(f.sidebar.frame,null);});
test('sidebar toggles repeatedly without drift',()=>{const f=fixture();for(let i=0;i<10;i++){f.sidebar.toggle();assert.equal(f.doc.body.children.length,1);f.sidebar.toggle();assert.equal(f.doc.body.children.length,0);}});
test('open twice does not duplicate iframe',()=>{const f=fixture();const frame=f.sidebar.open();assert.equal(f.sidebar.open(),frame);assert.equal(f.doc.body.children.length,1);assert.equal(f.doc.body.children[0].children.length,1);});
test('closing restores layout class and preserves existing classes',()=>{const f=fixture();f.sidebar.open();f.sidebar.close();assert.deepEqual([...f.classes],['existing']);});
test('iframe is local extension panel',()=>{const f=fixture();assert.equal(f.sidebar.open().src,'chrome-extension://kurukin/panel/index.html');});
test('close cancellation hook executes',()=>{const f=fixture();f.sidebar.open();f.sidebar.close();assert.equal(f.closed,1);});
