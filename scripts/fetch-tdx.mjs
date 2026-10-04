const PAGE_SIZES = [1000, 200, 100, 30];

async function fetchPage(src, token, top, skip) {
  const url = `${BASE}/${src.path}?$top=${top}&$skip=${skip}`;
  const res = await fetch(url, { headers: { authorization: `Bearer ${token}` } });
  if (!res.ok) {
    const body = (await res.text()).slice(0, 500);
    return { ok: false, status: res.status, body };
  }
  const json = await res.json();
  return { ok: true, items: Array.isArray(json.value) ? json.value : [] };
}

async function fetchAll(src, token) {
  const seen = new Set();
  const all = [];

  // 找出 API 接受的每頁筆數
  let pageSize = null;
  let first = null;
  for (const size of PAGE_SIZES) {
    const r = await fetchPage(src, token, size, 0);
    if (r.ok) { pageSize = size; first = r.items; break; }
    console.log(`${src.path}: $top=${size} rejected -> HTTP ${r.status} ${r.body}`);
  }
  if (!pageSize) throw new Error(`${src.path}: no page size accepted`);
  console.log(`${src.path}: using $top=${pageSize}`);

  const add = items => {
    let added = 0;
    for (const it of items) {
      const id = it[src.idField];
      if (id && !seen.has(id)) { seen.add(id); all.push(it); added++; }
    }
    return added;
  };

  add(first);
  let skip = first.length;
  while (first.length === pageSize && all.length < RAW_LIMIT) {
    await sleep(300);
    const r = await fetchPage(src, token, pageSize, skip);
    if (!r.ok) {
      console.log(`${src.path}: $skip=${skip} failed -> HTTP ${r.status} ${r.body}`);
      break;                       // 保留已抓到的資料
    }
    if (r.items.length === 0 || add(r.items) === 0) break;
    skip += r.items.length;
    if (r.items.length < pageSize) break;
  }
  return all;
}