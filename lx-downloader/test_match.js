// 测试修复后的匹配逻辑
function normalizeKeyword(keyword) {
  if (!keyword) return '';
  return String(keyword)
    .replace(/\(\s*Live\s*\)/gi, '')
    .replace(/\([^)]*\)/g, '')
    .replace(/\s+/g, '')
    .replace(/[!-/:-@\[-`{-~]/g, '')
    .trim()
    .toLowerCase();
}
function titleMatch(a, b) {
  const na = normalizeKeyword(a);
  const nb = normalizeKeyword(b);
  if (!na || !nb) return true;
  if (na === nb) return true;
  const diff = Math.abs(na.length - nb.length);
  if (diff <= 2 && (na.includes(nb) || nb.includes(na))) return true;
  return false;
}
const cases = [
  ['モニタリング -Instrumental-', 'モニタリング'],
  ['チェリーポップ', 'チェリーポップ'],
  ['0.0000%', '0.0000'],
  ['モニタリング', 'モニタリング'],
  ['ヴァンパイア', 'モニタリング'],
  ['モニタリング(Remix)', 'モニタリング'],
  ['青花瓷', '青花瓷'],
  ['Say So', 'Say So'],
  ['サイン', 'サイン'],
  ['黑色柳丁', '黑色柳丁 (Live)'],
];
for (const [a, b] of cases) {
  console.log(JSON.stringify(a), 'vs', JSON.stringify(b), '=>', titleMatch(a, b));
}
