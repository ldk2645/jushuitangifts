const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync('frontend/app.js', 'utf8');
const code = source.slice(source.indexOf('function normalizeGift'), source.indexOf('function giftNames'));
function matcher(catalog, allowed = catalog.map(g => g.id)) {
  const box = {giftCatalog: catalog, selectedGiftGroup: '', giftGroups: {revision: 1}, catalogVersion: 'test', scopeInvalid: false,
    scopedGifts: () => catalog.filter(g => allowed.includes(g.id))};
  vm.createContext(box); vm.runInContext(code, box);
  return text => JSON.parse(JSON.stringify(box.matchGifts(text)));
}
const gifts = [
  {id:'00001',name:'小米充电宝 10000mAh 22.5W',aliases:[]},
  {id:'power1',name:'小充电宝',aliases:[]}, {id:'power2',name:'10000mAh 22.5W',aliases:[]},
  {id:'a',name:'杯子',aliases:['口袋杯']}, {id:'b',name:'背包',aliases:[]},
  {id:'c',name:'同名',aliases:['唯一码']}, {id:'d',name:'同名',aliases:[]},
  {id:'e',name:'Xiaomi Pad 9 保护壳',aliases:[]},
  {id:'f',name:'套装，黑色',aliases:[]}, {id:'g',name:'Type-C+耳机',aliases:[]},
  {id:'h',name:'红色 型号 A-1',aliases:[]}, {id:'i',name:'红色 型号 A1',aliases:[]},
];
const match = matcher(gifts, gifts.filter(g=>g.id!=='e').map(g=>g.id));
const cases = [
  ['小充电宝 10000mAh 22.5W',[],['小充电宝 10000mAh 22.5W']],
  ['Xiaomi Pad 9 保护壳',[],['Xiaomi Pad 9 保护壳']], ['同名',[],['同名']],
  ['00001',['00001'],[]], ['００００１',['00001'],[]], ['唯一码',['c'],[]],
  ['杯子 背包',['a','b'],[]], ['杯子，背包',['a','b'],[]], ['杯子\n背包',['a','b'],[]],
  ['杯子 未知',[],['杯子 未知']], ['背保',[],['背保']],
  ['套装，黑色',['f'],[]], ['套装，黑色、杯子',['f','a'],[]], ['Type-C+耳机',['g'],[]],
  ['红色 型号 A-1',['h'],[]], ['红色 型号 A1',['i'],[]], ['红色 型号 A-2',[],['红色 型号 A-2']],
];
for (const [input, ids, unmatched] of cases) assert.deepEqual(match(input), {ids, unmatched}, input);
for (const size of [100,1000]) for (let i=0;i<size;i++) assert.deepEqual(match('杯子 背包'),{ids:['a','b'],unmatched:[]});
const ambiguous = matcher([{id:'1',name:'A',aliases:[]},{id:'2',name:'B C',aliases:[]},{id:'3',name:'A B',aliases:[]},{id:'4',name:'C',aliases:[]}]);
assert.deepEqual(ambiguous('A B C'), {ids:[],unmatched:['A B C']});
if (process.argv[2]) {
  const catalog = JSON.parse(fs.readFileSync(process.argv[2]+'/gifts.json','utf8'));
  const groups = JSON.parse(fs.readFileSync(process.argv[2]+'/gift_groups.local.json','utf8'));
  for (const group of ['xiaomi-phone','xiaomi-ecosystem','honor']) {
    const scoped = matcher(catalog, catalog.filter(g=>(groups.memberships[g.id]||[]).includes(group)).map(g=>g.id));
    for (const input of group==='honor' ? ['荣耀亲选 LCHSE 耳夹式耳机 黑色'] : ['小充电宝 10000mAh 22.5W','Xiaomi Pad 9 保护壳']) {
      assert.deepEqual(scoped(input),{ids:[],unmatched:[input]},group+': '+input);
    }
  }
  console.log('Read-only supplied catalog: reported safety examples passed (14-case file absent).');
}
console.log(`${cases.length+1} synthetic cases and 1100 deterministic repeated rows passed.`);
