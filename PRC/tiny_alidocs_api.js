(function () {
  if (window.alidocs) return;

  const api = {};
  const JSON_HEADERS = { 'content-type': 'application/json;charset=UTF-8' };
  let accessTokenCache = null;
  let corpIdCache = null;
  const taskMeta = new Map(); // taskId -> { kind: 'pdf'|'xlsx', dentryKey, docKey }

  async function safeFetch(url, opts) {
    const res = await fetch(url, opts);
    const ct = res.headers.get('content-type') || '';
    const isJson = ct.includes('application/json');
    const data = isJson ? await res.json() : await res.text();
    if (!res.ok) {
      throw new Error(typeof data === 'string' ? data : (data && (data.message || data.msg)) || ('HTTP ' + res.status));
    }
    return data;
  }

  function normalizeOk(data) {
    return { ok: true, data };
  }
  function normalizeErr(e) {
    return { ok: false, error: String(e && e.message || e) };
  }

  function getDingWebAppVersion() {
    return '4.85.4';
  }

  function randomStr(len, base) {
    const chars = base || '0192837465abcdefghijklmnopqrstuvwxyz';
    let s = '';
    for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random() * chars.length)];
    return s;
  }

  async function ensureAuthHeaders(includeCorp = true) {
    // 获取 A-Token
    if (!accessTokenCache) {
      const d = await safeFetch('/portal/api/v1/token/getAccessToken', { method: 'POST', credentials: 'include' });
      if (!d || !d.isSuccess || !d.data || !d.data.accessToken) throw new Error('getAccessToken failed');
      accessTokenCache = { accessToken: d.data.accessToken };
    }
    // 获取 corp-id
    if (includeCorp && !corpIdCache) {
      const cookieCorp = (document.cookie.split(';').map(s => s.trim()).find(s => s.startsWith('portal_corp_id=')) || '').split('=').pop();
      if (cookieCorp) {
        corpIdCache = cookieCorp;
      } else {
        const u = await safeFetch('/api/users/getUserInfo', { method: 'POST', credentials: 'include' });
        if (!u || !u.isSuccess) throw new Error('getUserInfo failed');
        const mainOrg = (u.data && u.data.orgs || []).find(x => x.isMainOrg);
        corpIdCache = mainOrg && mainOrg.corpId;
      }
    }
    const headers = { 'A-Token': accessTokenCache.accessToken };
    if (includeCorp && corpIdCache) headers['corp-id'] = corpIdCache;
    return headers;
  }

  // 复用已验证的 getAccessToken 形态（同源）
  api.getAccessToken = async function () {
    try {
      await ensureAuthHeaders(true);
      return normalizeOk({ accessToken: accessTokenCache.accessToken });
    } catch (e) { return normalizeErr(e); }
  };

  api.getUserInfo = async function () {
    try {
      const d = await safeFetch('/api/users/getUserInfo', { method: 'POST', credentials: 'include' });
      if (!d || !d.isSuccess) throw new Error('getUserInfo failed');
      return normalizeOk({ userId: d.data && d.data.userId, nick: d.data && d.data.nick });
    } catch (e) { return normalizeErr(e); }
  };

  function extractNodeId(input) {
    if (!input) return '';
    try {
      if (/^https?:\/\//i.test(input)) {
        const u = new URL(input);
        const parts = u.pathname.split('/').filter(Boolean);
        const idx = parts.findIndex(p => p === 'nodes');
        if (idx >= 0 && parts[idx + 1]) return parts[idx + 1];
        return parts[parts.length - 1] || '';
      }
      return String(input);
    } catch (_) { return String(input); }
  }

  api.resolveNode = async function (urlOrNodeId) {
    try {
      const nodeId = extractNodeId(urlOrNodeId);
      // 尝试读取节点元信息以确认类型
      const meta = await api.getNodeMeta(nodeId);
      if (!meta.ok) return meta;
      const t = meta.data.type || (meta.data.isFolder ? 'folder' : 'doc');
      return normalizeOk({ nodeId, type: t });
    } catch (e) { return normalizeErr(e); }
  };

  api.getNodeMeta = async function (nodeId) {
    try {
      const headers = await ensureAuthHeaders(true);
      const d = await safeFetch(`/box/api/v2/dentry/info?dentryUuid=${encodeURIComponent(nodeId)}`, { method: 'GET', credentials: 'include', headers });
      if (!d || !d.isSuccess) throw new Error('dentry/info failed');
      const info = d.data;
      const extShort = info.extension; // adoc/axls/...
      const t = info.dentryType === 'folder' ? 'folder' : (extShort === 'adoc' ? 'doc' : (extShort === 'axls' ? 'sheet' : inferTypeByContent(info.contentType)));
      const ext = extShort ? ('.' + extShort) : (t === 'sheet' ? '.axls' : (t === 'doc' ? '.adoc' : undefined));
      return normalizeOk({ id: info.dentryUuid, name: info.name, type: t, ext, extension: extShort, hasChildren: !!info.hasChildren, updatedAt: info.gmtModified, docKey: info.docKey, dentryKey: info.dentryKey, contentType: info.contentType });
    } catch (e) { return normalizeErr(e); }
  };

  api.listChildren = async function (parentId, cursor) {
    try {
      const headers = await ensureAuthHeaders(true);
      const qs = new URLSearchParams();
      qs.set('pageSize', '100');
      qs.set('dentryUuid', parentId);
      if (cursor) qs.set('loadMoreId', cursor);
      const d = await safeFetch(`/box/api/v2/dentry/list?` + qs.toString(), { method: 'GET', credentials: 'include', headers });
      if (!d || !d.isSuccess) throw new Error('dentry/list failed');
      const items = (d.data && d.data.children || []).map(x => {
        const extShort = x.extension; // 例如 adoc/axls
        const t = x.dentryType === 'folder' ? 'folder' : (extShort === 'adoc' ? 'doc' : (extShort === 'axls' ? 'sheet' : inferTypeByContent(x.contentType)));
        const e = extShort ? ('.' + extShort) : (t === 'sheet' ? '.axls' : (t === 'doc' ? '.adoc' : undefined));
        return {
          id: x.dentryUuid,
          name: x.name,
          type: t,
          ext: e,
          extension: extShort,
          hasChildren: !!x.hasChildren,
          updatedTime: x.updatedTime,
          docKey: x.docKey,
          dentryKey: x.dentryKey,
          contentType: x.contentType
        };
      });
      const nextCursor = d.data && d.data.loadMoreId;
      return normalizeOk({ items, nextCursor: nextCursor || undefined });
    } catch (e) { return normalizeErr(e); }
  };

  function inferTypeByContent(contentType) {
    if (!contentType) return 'other';
    const ct = String(contentType).toLowerCase();
    // 仅严格识别 Alidocs 内置类型，避免误判 pptx 等为 doc
    if (ct.includes('application/x-alidocs-sheet')) return 'sheet';
    if (ct.includes('application/x-alidocs-word')) return 'doc';
    return 'other';
  }

  async function getDocumentData(dentryKey, docKey, source) {
    const headers = await ensureAuthHeaders(true);
    headers['a-dentry-key'] = dentryKey || '';
    headers['a-doc-key'] = docKey || '';
    const body = { dentryKey, pageMode: 2, fetchBody: true };
    if (source) body['source'] = source;
    const d = await safeFetch('/api/document/data', { method: 'POST', credentials: 'include', headers: { ...headers, ...JSON_HEADERS }, body: JSON.stringify(body) });
    if (!d || !d.isSuccess) throw new Error('document/data failed');
    return d.data;
  }

  async function uploadInfo(docKey, resourceName, size) {
    const headers = await ensureAuthHeaders(false);
    headers['a-doc-key'] = docKey;
    headers['a-host-doc-key'] = '';
    const d = await safeFetch('/core/api/resources/9/upload_info', { method: 'POST', credentials: 'include', headers: { ...headers, ...JSON_HEADERS }, body: JSON.stringify({ contentType: '', resourceName, size }) });
    if (!d || !d.isSuccess) throw new Error('upload_info failed');
    return d.data;
  }

  async function uploadToOSS(url, body) {
    return new Promise((resolve, reject) => {
      try {
        const xhr = new XMLHttpRequest();
        xhr.open('PUT', url, true);
        // 与仓库实现一致，Content-Type 置空字符串
        xhr.setRequestHeader('Content-Type', '');
        xhr.withCredentials = false; // 预签名URL禁止携带凭据
        xhr.onload = function () {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve(true);
          } else {
            reject(new Error('OSS PUT failed: ' + xhr.status + ' ' + xhr.responseText));
          }
        };
        xhr.onerror = function () { reject(new Error('OSS PUT network error')); };
        xhr.send(body);
      } catch (e) {
        reject(e);
      }
    });
  }

  async function getDocOpenToken(dentryKey, docKey, corpId) {
    if (!window.lwpClient) throw new Error('lwpClient not available for docOpenToken');
    const payload = {
      'A-DENTRY-KEY': dentryKey,
      'utm_source': 'portal',
      'utm_medium': 'portal_space_file_tree',
      'SOURCE_DOC_APP': 'doc',
      'A-DOC-KEY': docKey,
      'mid': randomStr(25, '0192837465') + ' 0'
    };
    const arr = [corpId, docKey];
    const resp = await window.lwpClient.sendMsg('/r/Adaptor/DingTalkDocI/getDocOpenToken', payload, arr);
    const code = resp && resp.code;
    if (code !== 200) throw new Error('getDocOpenToken failed: ' + JSON.stringify(resp));
    return resp.body;
  }

  api.createExportTask = async function (nodeId, target /* 'pdf'|'xlsx' */) {
    try {
      // 先拿元信息
      const meta = await api.getNodeMeta(nodeId);
      if (!meta.ok) throw new Error(meta.error || 'getNodeMeta failed');
      const info = meta.data;

      if (target === 'pdf') {
        // 获取文档数据并组装 PDF 导出 payload（参考仓库实现）
        const docData = await getDocumentData(info.dentryKey, info.docKey);
        const fileMeta = docData.fileMetaInfo || {};
        const userInfo = docData.userInfo || {};
        const creatorNick = fileMeta.creator && fileMeta.creator.nick;
        const cid = fileMeta.corpId;
        const org = (userInfo.orgs || []).find(o => o.corpId === cid);
        let nick = creatorNick;
        let corpName = org && org.name;
        let watermark = 'CLOSE';
        if (fileMeta.securityPolicyControl && fileMeta.securityPolicyControl.watermarkEnable) {
          watermark = 'OPEN';
          nick = fileMeta.securityPolicyControl.watermarkText && fileMeta.securityPolicyControl.watermarkText.rowTwo;
          corpName = fileMeta.securityPolicyControl.watermarkText && fileMeta.securityPolicyControl.watermarkText.rowOne;
        }
        // 获取 docOpenToken（必要）
        const openTokenBody = await getDocOpenToken(info.dentryKey, info.docKey, cid);

        // 去掉文件名后缀（与仓库 api.js 保持一致）
        let fileName = info.name;
        if (fileName.includes('.')) {
          const ns = fileName.split('.');
          ns.pop();
          fileName = ns.join('.').trim();
        }

        const uploadBody = JSON.stringify({
          asl: docData.documentContent.checkpoint.content,
          optionsString: JSON.stringify({
            openToken: { docOpenToken: openTokenBody, corpId: cid, docKey: info.docKey },
            isNew: true,
            customConfig: { content: 'ONLYCONTENT', mode: 'PORTRAIT', watermark, nick, corpName, link: '', enableTableAutofitWidth: true },
            fileName: fileName,
            showDocTitle: true,
            ctxVersion: docData.documentContent.checkpoint.baseVersion,
            printStyle: { backgroundColor: 'var(--we_bg_default_color, rgba(255, 255, 255, 1))' },
            version: 1,
            appVersion: getDingWebAppVersion(),
            exportType: 'pdf',
            corpId: cid,
            lang: 'zh-CN'
          })
        });

        const up = await uploadInfo(info.docKey, info.docKey, uploadBody.length);
        await uploadToOSS(up.uploadUrl, uploadBody);

        const headers = await ensureAuthHeaders(false);
        headers['a-dentry-key'] = info.dentryKey;
        headers['a-doc-key'] = info.docKey;
        const job = await safeFetch('/api/v2/files/createExportJob', { method: 'POST', credentials: 'include', headers: { ...headers, ...JSON_HEADERS }, body: JSON.stringify({ scene: 'normal', storagePath: up.storagePath }) });
        if (!job || !job.isSuccess) throw new Error('createExportJob failed');
        const jobId = job.data.jobId;
        taskMeta.set(jobId, { kind: 'pdf', dentryKey: info.dentryKey, docKey: info.docKey, initialUrl: job.data.url });
        return normalizeOk({ taskId: jobId });
      }

      if (target === 'xlsx') {
        // 钉表导出 xlsx
        const docData = await getDocumentData(info.dentryKey, info.docKey);
        const content = JSON.parse(docData.documentContent.checkpoint.content);
        const contentdata = { ...content, setting: { ...(content.setting || {}), calc: { enableFormulaStatus: true } } };
        const uploadBody = JSON.stringify({
          content: contentdata.content,
          customTabsMeta: contentdata.customTabsMeta,
          modules: { asyncFunctionCache: [], form: {}, dimensionMeta: {}, protectionRange: {}, follow: {}, tag: {}, dingtalkTask: [], merge: {}, mention: {}, appLock: {}, lock: {}, float: {}, filter: {}, dataValidation: {}, reaction: {}, reminder: {}, comment: {}, filterView: {}, pivotTable: {}, conditionalFormatting: {}, calc: { shared: { exprs: [] } }, externalLink: [], table: {}, definedName: [] },
          setting: contentdata.setting,
          sheetsMeta: contentdata.sheetsMeta,
          style: contentdata.style,
          tabs: contentdata.tabs,
          version: contentdata.version
        });

        const up = await uploadInfo(info.docKey, info.name, uploadBody.length);
        await uploadToOSS(up.uploadUrl, uploadBody);

        const headers = await ensureAuthHeaders(false);
        headers['a-dentry-key'] = info.dentryKey;
        headers['a-doc-key'] = info.docKey;
        const job = await safeFetch('/core/api/document/submitExportJob', { method: 'POST', credentials: 'include', headers: { ...headers, ...JSON_HEADERS }, body: JSON.stringify({ exportType: 'dingTalksheetToxlsx', storagePath: up.storagePath }) });
        if (!job || !job.isSuccess) throw new Error('submitExportJob failed');
        const jobId = job.data.jobId;
        taskMeta.set(jobId, { kind: 'xlsx', dentryKey: info.dentryKey, docKey: info.docKey });
        return normalizeOk({ taskId: jobId });
      }

      throw new Error('unsupported target');
    } catch (e) { return normalizeErr(e); }
  };

  api.getExportTask = async function (taskId) {
    try {
      const meta = taskMeta.get(taskId);
      if (!meta) throw new Error('unknown taskId');
      const headers = await ensureAuthHeaders(false);
      headers['a-dentry-key'] = meta.dentryKey;
      headers['a-doc-key'] = meta.docKey;
      if (meta.kind === 'pdf') {
        const r = await safeFetch(`/api/v2/files/queryExportStatus?jobId=${encodeURIComponent(taskId)}`, { method: 'GET', credentials: 'include', headers });
        if (!r || !r.isSuccess) throw new Error('queryExportStatus failed');
        const done = r.data.done;
        const url = meta.initialUrl || (r.data && r.data.url);
        return normalizeOk({ status: done ? 'success' : 'running', downloadUrl: done ? url : undefined });
      } else {
        const r = await safeFetch(`/core/api/document/queryExportJobInfo?jobId=${encodeURIComponent(taskId)}`, { method: 'GET', credentials: 'include', headers });
        if (!r || !r.isSuccess) throw new Error('queryExportJobInfo failed');
        const st = r.data.status;
        const url = r.data.ossUrl;
        if (st === 'success') return normalizeOk({ status: 'success', downloadUrl: url });
        if (st === 'failed') return normalizeOk({ status: 'failed' });
        return normalizeOk({ status: 'running' });
      }
    } catch (e) { return normalizeErr(e); }
  };

  // 直链下载：用于原始上传的非 Alidocs 类型（如 docx/xlsx/pdf）
  api.downloadDocument = async function (dentryUuid) {
    try {
      const headers = await ensureAuthHeaders(true);
      const qs = new URLSearchParams();
      qs.set('dentryUuid', dentryUuid);
      qs.set('supportDownloadTypes', 'URL_PRE_SIGNATURE,HTTP_TO_CENTER');
      qs.set('downloadType', 'URL_PRE_SIGNATURE');
      const d = await safeFetch(`/box/api/v2/file/download?` + qs.toString(), { method: 'GET', credentials: 'include', headers });
      if (!d || !d.isSuccess) throw new Error('file/download failed');
      const url = d.data && d.data.ossUrlPreSignatureInfo && d.data.ossUrlPreSignatureInfo.preSignUrls && d.data.ossUrlPreSignatureInfo.preSignUrls[0];
      if (!url) throw new Error('preSign url not found');
      return normalizeOk({ url });
    } catch (e) { return normalizeErr(e); }
  };

  // 暴露
  window.alidocs = api;
  //# sourceURL=tiny_alidocs_api.js
})();


