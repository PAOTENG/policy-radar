(() => {
  const API = "/api/v1/admin/kb";

  const RELATION_LABELS = {
    implements: "实施/细则",
    same_program: "同一项目",
    lists_under: "名单/公示隶属",
    funding_of: "资金来源",
    supersedes: "废止/替代",
    region_child: "下级地区",
    cites: "引用",
    related: "相关",
  };

  const zone = document.getElementById("uploadZone");
  const fileInput = document.getElementById("fileInput");
  const uploadHint = document.getElementById("uploadHint");
  const uploadMsg = document.getElementById("uploadMsg");
  const docBody = document.getElementById("docBody");
  const btnRefresh = document.getElementById("btnRefresh");
  const uploadedOnly = document.getElementById("uploadedOnly");
  const modal = document.getElementById("modal");
  const modalTitle = document.getElementById("modalTitle");
  const modalBody = document.getElementById("modalBody");

  if (!zone) return;

  function setMsg(text, ok) {
    uploadMsg.hidden = !text;
    uploadMsg.textContent = text || "";
    uploadMsg.className = "upload-msg " + (ok ? "ok" : "err");
  }

  function setZone(state, text) {
    zone.classList.remove("uploading", "success", "error");
    if (state) zone.classList.add(state);
    if (text) uploadHint.textContent = text;
  }

  function openModal(title, html) {
    modalTitle.textContent = title;
    modalBody.innerHTML = html;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
  }

  function closeModal() {
    modal.hidden = true;
    document.body.style.overflow = "";
  }

  modal.querySelectorAll("[data-close]").forEach((el) => {
    el.addEventListener("click", closeModal);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.hidden) closeModal();
  });

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function metaItem(k, v) {
    return `<div class="meta-item"><span class="k">${esc(k)}</span><span class="v">${esc(v || "—")}</span></div>`;
  }

  function renderResult(data) {
    const pipeline = (data.pipeline || [])
      .map((p) => `<span class="chip">${esc(p)}</span>`)
      .join("");

    const rels = data.relations || [];
    let relHtml;
    if (!rels.length) {
      relHtml = `<p class="muted">暂未匹配到关联边（库中可能缺少同类/同地区政策，或标题特征不足）。</p>`;
    } else {
      relHtml = `<div class="rel-list">${rels
        .map((r) => {
          const typeLabel = RELATION_LABELS[r.relation_type] || r.relation_type;
          const conf = Math.round((Number(r.confidence) || 0) * 100);
          const dir = r.direction === "in" ? "入边" : "出边";
          return `<article class="rel-card">
            <div class="rel-top">
              <span class="badge type">${esc(typeLabel)}</span>
              <span class="badge conf">置信度 ${conf}%</span>
              <span class="badge">${esc(dir)}</span>
              <span class="badge">#${esc(r.neighbor_id)}</span>
            </div>
            <p class="rel-title">${esc(r.neighbor_title)}</p>
            <p class="rel-ev">${esc(r.neighbor_region || "")}${r.evidence ? " · " + esc(r.evidence) : ""}</p>
          </article>`;
        })
        .join("")}</div>`;
    }

    return `
      <div class="pipeline">${pipeline}</div>
      <div class="meta-grid">
        ${metaItem("政策 ID", data.policy_id)}
        ${metaItem("文件名", data.file)}
        ${metaItem("标题", data.title)}
        ${metaItem("地区", data.region)}
        ${metaItem("类目", data.category_l1)}
        ${metaItem("发布单位", data.publisher)}
        ${metaItem("资助额度", data.funding_amount)}
        ${metaItem("截止日期", data.deadline)}
        ${metaItem("分块数", data.chunks)}
        ${metaItem("正文长度", data.total_chars != null ? data.total_chars + " 字" : "")}
        ${metaItem("关联边数", data.relation_count ?? rels.length)}
      </div>
      <h4 style="margin:0 0 10px;font-size:15px;">知识图谱关联</h4>
      ${relHtml}
    `;
  }

  async function uploadFile(file) {
    setZone("uploading", "正在处理：" + file.name);
    setMsg("解析、向量化与建图进行中，请稍候…", true);

    const fd = new FormData();
    fd.append("file", file);

    try {
      const res = await fetch(`${API}/upload`, { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || data.message || `HTTP ${res.status}`);
      }
      if (data.status === "skipped") {
        setZone("error", "点击重新选择文件");
        setMsg(data.reason || "已跳过", false);
        return;
      }
      setZone("success", "上传成功，可继续选择文件");
      setMsg(
        `✓ 已入库 #${data.policy_id}「${data.title}」，分块 ${data.chunks}，关联边 ${data.relation_count}`,
        true
      );
      openModal("上传成功 · 建图完成", renderResult(data));
      await loadDocs();
    } catch (e) {
      setZone("error", "点击重新选择文件");
      setMsg("错误：" + (e.message || e), false);
    } finally {
      fileInput.value = "";
    }
  }

  async function loadDocs() {
    docBody.innerHTML = `<tr><td colspan="7" class="empty">加载中…</td></tr>`;
    const only = uploadedOnly.checked ? "true" : "false";
    try {
      const res = await fetch(`${API}/documents?limit=50&uploaded_only=${only}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "加载失败");
      const docs = data.documents || [];
      if (!docs.length) {
        docBody.innerHTML = `<tr><td colspan="7" class="empty">暂无文档</td></tr>`;
        return;
      }
      docBody.innerHTML = docs
        .map(
          (d) => `<tr>
          <td>${esc(d.id)}</td>
          <td class="title-cell">${esc(d.title)}</td>
          <td>${esc(d.region || "—")}</td>
          <td>${esc(d.category_l1 || "—")}</td>
          <td>${esc(d.chunks)}</td>
          <td>${esc(d.relations)}</td>
          <td>
            <button class="btn-link" data-rel="${d.id}" type="button">查看关系</button>
            ${
              d.is_upload
                ? ` · <button class="btn-danger" data-del="${d.id}" type="button">删除</button>`
                : ""
            }
          </td>
        </tr>`
        )
        .join("");
    } catch (e) {
      docBody.innerHTML = `<tr><td colspan="7" class="empty">加载失败：${esc(e.message)}</td></tr>`;
    }
  }

  async function showRelations(id) {
    try {
      const res = await fetch(`${API}/documents/${id}/relations`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "查询失败");
      openModal(`知识图谱 · #${data.policy_id}`, renderResult({
        ...data,
        policy_id: data.policy_id,
        title: data.title,
        region: data.region,
        category_l1: data.category_l1,
        relation_count: data.relation_count,
        relations: data.relations,
        pipeline: ["已入库政策", `关联边 ×${data.relation_count}`],
      }));
    } catch (e) {
      openModal("查询失败", `<p class="muted">${esc(e.message)}</p>`);
    }
  }

  async function deleteDoc(id) {
    if (!confirm(`确定删除政策 #${id}？将同时删除分块与关联边。`)) return;
    try {
      const res = await fetch(`${API}/documents/${id}`, { method: "DELETE" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "删除失败");
      await loadDocs();
    } catch (e) {
      alert(e.message || e);
    }
  }

  zone.addEventListener("click", () => fileInput.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener("change", () => {
    const f = fileInput.files?.[0];
    if (f) uploadFile(f);
  });

  ;["dragenter", "dragover"].forEach((ev) => {
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.add("uploading");
    });
  });
  ;["dragleave", "drop"].forEach((ev) => {
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.remove("uploading");
    });
  });
  zone.addEventListener("drop", (e) => {
    const f = e.dataTransfer?.files?.[0];
    if (f) uploadFile(f);
  });

  btnRefresh.addEventListener("click", loadDocs);
  uploadedOnly.addEventListener("change", loadDocs);
  docBody.addEventListener("click", (e) => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    const rel = t.getAttribute("data-rel");
    const del = t.getAttribute("data-del");
    if (rel) showRelations(rel);
    if (del) deleteDoc(del);
  });

  loadDocs();
})();
