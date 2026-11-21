// Docs page: builds the table of contents and the per-section "Copy" buttons.
// Sections are copied as Markdown so they can be pasted straight into an LLM chat.
(function () {
  const sections = [...document.querySelectorAll("section.doc")];
  const toc = document.getElementById("toc");
  const box = document.getElementById("copybox");

  // ---- DOM -> Markdown (just the elements the docs use) ----
  function inline(node) {
    let out = "";
    for (const n of node.childNodes) {
      if (n.nodeType === Node.TEXT_NODE) { out += n.textContent.replace(/\s+/g, " "); continue; }
      if (n.nodeType !== Node.ELEMENT_NODE || n.matches(".nocopy, button")) continue;
      const tag = n.tagName.toLowerCase(), t = inline(n);
      if (tag === "code") out += "`" + n.textContent + "`";
      else if (tag === "b" || tag === "strong") out += t.trim() ? `**${t.trim()}**` : t;
      else if (tag === "a") {
        const href = n.getAttribute("href") || "";
        const abs = href.startsWith("#") ? location.origin + location.pathname + href : new URL(href, location.href).href;
        out += `[${t.trim()}](${abs})`;
      } else if (tag === "br") out += "\n";
      else out += t;
    }
    return out;
  }

  function list(el, depth) {
    const ordered = el.tagName === "OL";
    let i = 0, out = "";
    for (const li of el.children) {
      if (li.tagName !== "LI") continue;
      i++;
      const own = document.createElement("div");
      const nested = [];
      for (const c of li.childNodes) {
        if (c.nodeType === Node.ELEMENT_NODE && (c.tagName === "UL" || c.tagName === "OL")) nested.push(c);
        else own.appendChild(c.cloneNode(true));
      }
      out += "  ".repeat(depth) + (ordered ? `${i}. ` : "- ") + inline(own).trim() + "\n";
      for (const n of nested) out += list(n, depth + 1);
    }
    return out;
  }

  function table(el) {
    const rows = [...el.querySelectorAll("tr")].map(tr => [...tr.children].map(td => inline(td).trim().replace(/\|/g, "\\|")));
    if (!rows.length) return "";
    const head = rows[0], body = rows.slice(1);
    return [`| ${head.join(" | ")} |`, `| ${head.map(() => "---").join(" | ")} |`, ...body.map(r => `| ${r.join(" | ")} |`)].join("\n") + "\n";
  }

  function block(el) {
    let out = "";
    for (const n of el.children) {
      if (n.matches(".nocopy, button")) continue;
      const tag = n.tagName.toLowerCase();
      if (/^h[1-6]$/.test(tag)) out += "#".repeat(+tag[1]) + " " + inline(n).trim() + "\n\n";
      else if (tag === "p") out += inline(n).trim() + "\n\n";
      else if (tag === "ul" || tag === "ol") out += list(n, 0) + "\n";
      else if (tag === "pre") out += "```\n" + n.textContent.replace(/\n+$/, "") + "\n```\n\n";
      else if (tag === "table") out += table(n) + "\n";
      else out += block(n);
    }
    return out;
  }

  const eventName = document.title.split(" · ").pop();
  const sectionMd = (s) => block(s).trim();
  const header = `<!-- ${eventName}: docs & rules, from ${location.origin}/docs. Numbers reflect the current settings. -->\n\n`;

  async function copy(text, btn) {
    const label = btn.textContent;
    if (await copyText(text)) {
      btn.textContent = "Copied ✓";
      setTimeout(() => (btn.textContent = label), 1800);
    } else {
      // No clipboard access (e.g. plain http): show the text selected so Ctrl/Cmd+C works.
      box.hidden = false; box.value = text;
      btn.closest("section, .docs-head").after(box);
      box.focus(); box.select();
      btn.textContent = "Press Ctrl/Cmd+C";
      setTimeout(() => (btn.textContent = label), 3000);
    }
  }

  // ---- table of contents + buttons ----
  for (const s of sections) {
    const h = s.querySelector("h2");
    const li = document.createElement("li");
    li.innerHTML = `<a href="#${s.id}">${h.textContent}</a>`;
    toc.appendChild(li);

    const bar = document.createElement("div");
    bar.className = "doc-title";
    h.replaceWith(bar);
    bar.appendChild(h);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copybtn nocopy";
    btn.title = "Copy this section as Markdown (for an LLM)";
    btn.textContent = "Copy";
    btn.onclick = () => copy(header + sectionMd(s), btn);
    bar.appendChild(btn);
  }
  document.querySelector("[data-copy-all]").onclick = (ev) =>
    copy(header + sections.map(sectionMd).join("\n\n---\n\n"), ev.currentTarget);

  // highlight the section being read
  const links = new Map([...toc.querySelectorAll("a")].map(a => [a.getAttribute("href").slice(1), a]));
  const seen = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) {
      links.forEach(a => a.classList.remove("on"));
      links.get(e.target.id)?.classList.add("on");
    }
  }, { rootMargin: "-10% 0px -80% 0px" });
  sections.forEach(s => seen.observe(s));
})();
