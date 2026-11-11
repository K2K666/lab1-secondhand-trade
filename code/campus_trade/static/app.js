"use strict";

const app = document.querySelector("#app");
const navigation = document.querySelector("#navigation");
const accountArea = document.querySelector("#account");
const announcement = document.querySelector("#announcement");
const state = { account: null, categories: [], flash: "", renderId: 0 };
const productStatuses = {
  draft: "草稿", pending_review: "待审核", published: "已发布", rejected: "已驳回",
  downlisted: "已下架", reserved: "已被预约", sold: "已售出", violation: "违规下架"
};
const reservationStatuses = {
  pending: "待卖家确认", confirmed: "已确认", canceled: "已取消",
  rejected: "已拒绝", completed: "已完成"
};
const roleLabels = { member: "普通用户", admin: "管理员", super: "超级管理员" };
const numberFormat = new Intl.NumberFormat("zh-CN");
const timeFormat = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "short"
});

function node(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value == null || value === false) continue;
    if (name === "on") {
      for (const [event, listener] of Object.entries(value)) element.addEventListener(event, listener);
    } else {
      element.setAttribute(name, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

function pathLink(label, path, kind = "") {
  return node("a", { href: `#${path}`, class: kind ? `button ${kind}` : undefined }, label);
}

function button(label, kind = "", listener = null, attrs = {}) {
  return node("button", {
    class: `button ${kind}`.trim(), type: "button",
    on: listener ? { click: listener } : null, ...attrs
  }, label);
}

function withQuery(path, params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value != null) query.set(key, String(value));
  }
  return query.size ? `${path}?${query}` : path;
}

function segment(value) { return encodeURIComponent(String(value)); }

function currentRoute() {
  const [path, query = ""] = (location.hash.slice(1) || "/").split("?");
  return { path: path.startsWith("/") ? path : "/", params: Object.fromEntries(new URLSearchParams(query)) };
}

function go(path, flash = "") {
  state.flash = flash;
  if (location.hash === `#${path}` || (!location.hash && path === "/")) {
    render();
  } else {
    location.hash = path;
  }
}

class HttpError extends Error {
  constructor(response, data) {
    super(data.error || `请求失败（HTTP ${response.status}）`);
    this.status = response.status;
    this.code = data.code;
  }
}

async function api(path, body) {
  const options = { credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options).catch(() => {
    throw new Error("无法连接到系统。请确认服务正在运行，再重试。");
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new HttpError(response, data);
  return data;
}

function tell(message) {
  announcement.textContent = message;
}

function errorBox() {
  return node("p", { class: "error", role: "alert", hidden: true });
}

function setError(box, error) {
  box.textContent = error ? error.message : "";
  box.hidden = !error;
}

function busyButton(control, error, action) {
  return async () => {
    if (control.disabled) return;
    control.disabled = true;
    setError(error, null);
    try {
      await action();
    } catch (failure) {
      setError(error, failure);
    } finally {
      control.disabled = false;
    }
  };
}

function bindForm(form, submit, error, action) {
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    busyButton(submit, error, () => action(new FormData(form)))();
  });
}

function field(label, name, attrs = {}, help = "") {
  return node("label", { class: "field" }, label,
    node("input", { name, ...attrs }), help ? node("small", {}, help) : null);
}

function area(label, name, value = "", attrs = {}) {
  const input = node("textarea", { name, ...attrs });
  input.value = value ?? "";
  return node("label", { class: "field" }, label, input);
}

function pick(label, name, values, chosen = "", attrs = {}) {
  const select = node("select", { name, ...attrs }, values.map(([value, text]) => node("option", { value }, text)));
  select.value = String(chosen ?? "");
  return node("label", { class: "field" }, label, select);
}

function formString(data, key) { return String(data.get(key) ?? ""); }
function money(value) { return value == null ? "未定价" : `¥${(Number(value) / 100).toFixed(2).replace(/\.00$/, "")}`; }
function time(value) { return value ? timeFormat.format(new Date(Number(value) * 1000)) : "—"; }

function asCents(value, label) {
  const text = String(value).trim();
  if (!text) return "";
  if (!/^\d{1,8}(\.\d{1,2})?$/.test(text)) throw new Error(`${label}请输入最多两位小数的金额。`);
  const [whole, fraction = ""] = text.split(".");
  return Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
}

function labelStatus(value, labels = productStatuses) {
  const tone = ["pending", "pending_review"].includes(value) ? "warning"
    : ["violation", "rejected", "canceled"].includes(value) ? "danger"
      : ["draft", "downlisted", "sold", "completed"].includes(value) ? "neutral" : "";
  return node("span", { class: `badge ${tone}`.trim() }, labels[value] || value || "—");
}

function heading(title, subtitle = "", action = null) {
  return node("header", { class: "page-heading" },
    node("div", {}, node("h1", {}, title), subtitle ? node("p", {}, subtitle) : null), action);
}

function panel(title, ...children) {
  return node("section", { class: "panel" }, title ? node("h2", {}, title) : null, ...children);
}

function empty(label) { return node("p", { class: "empty" }, label); }
function pageContent(...content) { return node("div", { class: "stack" }, ...content); }
function actions(...content) { return node("div", { class: "actions" }, ...content); }

function table(headers, rows, emptyLabel = "暂无记录") {
  if (!rows.length) return empty(emptyLabel);
  return node("div", { class: "table-wrap", tabindex: "0", "aria-label": "记录表格" },
    node("table", {}, node("thead", {}, node("tr", {},
      headers.map((label) => node("th", { scope: "col" }, label)))),
    node("tbody", {}, rows.map((row) => node("tr", {}, row.map((cell) => node("td", {}, cell)))))));
}

function pager(payload, linkFor) {
  const current = Number(payload.page);
  const last = Math.max(1, Math.ceil(Number(payload.total) / Number(payload.page_size)));
  if (last <= 1) return null;
  return node("nav", { class: "pagination", "aria-label": "分页" },
    current > 1 ? pathLink("上一页", linkFor(current - 1), "secondary") : button("上一页", "", null, { disabled: true }),
    node("span", {}, `第 ${current} / ${last} 页`),
    current < last ? pathLink("下一页", linkFor(current + 1), "secondary") : button("下一页", "", null, { disabled: true }));
}

function chooseLinks(items, selected) {
  return node("nav", { class: "tabs", "aria-label": "功能切换" },
    items.map(([key, label, path]) => node("a", {
      href: `#${path}`, class: "button", "aria-current": key === selected ? "page" : undefined
    }, label)));
}

function requestKey() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("");
}

function actionDialog({ title, detail, confirm = "确认", reason = "", reasonRequired = false, danger = false, run }) {
  const error = errorBox();
  const submit = button(confirm, danger ? "danger" : "primary", null, { type: "submit" });
  const dialog = node("dialog", { class: "dialog", "aria-labelledby": "action-dialog-title" });
  const cancel = button("返回", "", () => dialog.close());
  const form = node("form", { class: "stack" },
    reason ? area(reason, "reason", "", { required: reasonRequired, maxlength: 500 }) : null,
    error, actions(cancel, submit));
  if (reasonRequired) {
    const input = form.querySelector("textarea");
    submit.disabled = true;
    input.addEventListener("input", () => { submit.disabled = !input.value.trim(); });
  }
  let working = false;
  const key = requestKey();
  bindForm(form, submit, error, async (data) => {
    working = true;
    cancel.disabled = true;
    if (reason) form.querySelector("textarea").readOnly = true;
    try {
      await run(formString(data, "reason").trim(), key);
      dialog.close();
    } finally {
      working = false;
      cancel.disabled = false;
      if (reason) form.querySelector("textarea").readOnly = false;
    }
  });
  dialog.addEventListener("cancel", (event) => { if (working) event.preventDefault(); });
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  dialog.append(node("h2", { id: "action-dialog-title" }, title), detail, form);
  document.body.append(dialog);
  dialog.showModal();
}

function renderNavigation(route) {
  const user = state.account;
  const admin = user && ["admin", "super"].includes(user.role);
  const items = user?.must_change ? []
    : admin ? [["集市", "/"], ["审核治理", "/governance"], ["平台统计", "/stats"], ["平台日志", "/logs"]]
      : user ? [["集市", "/"], ["我的商品", "/selling"], ["预约交易", "/reservations"],
        ["卖家统计", "/stats"], ["操作历史", "/logs"], ["站内通知", "/notifications"]]
        : [["集市", "/"]];
  if (user?.role === "super" && !user.must_change) items.push(["账号管理", "/admin-accounts"]);
  navigation.replaceChildren(...items.map(([label, path]) => node("a", {
    href: `#${path}`, "aria-current": route.path === path || (path !== "/" && route.path.startsWith(path + "/")) ? "page" : undefined
  }, label)));
  if (!user) {
    accountArea.replaceChildren(pathLink("登录", "/login", "secondary"));
    return;
  }
  const logout = button("退出", "secondary");
  logout.addEventListener("click", async () => {
    if (logout.disabled) return;
    logout.disabled = true;
    try {
      await api("/api/auth/logout", {});
      state.account = null;
      go("/", "已退出登录。");
    } catch (error) {
      tell(error.message);
      app.prepend(node("p", { class: "error", role: "alert" }, error.message));
      logout.disabled = false;
    }
  });
  accountArea.replaceChildren(
    user.must_change ? node("span", { class: "account-label" }, user.nickname)
      : node("a", { href: "#/profile", title: roleLabels[user.role] || "账号" },
        user.avatar ? node("span", { "aria-hidden": "true" }, `${user.avatar} `) : null, user.nickname),
    logout);
}

function authCard(title, subtitle, form, footer, register = false) {
  return node("section", { class: `auth-panel${register ? " register" : ""}` },
    node("h1", {}, title), node("p", { class: "quiet" }, subtitle), form, footer);
}

const phoneInput = { type: "tel", autocomplete: "tel", inputmode: "numeric", maxlength: 11, pattern: "[0-9]{11}", required: true };
const newPasswordInput = { type: "password", autocomplete: "new-password", minlength: 8, maxlength: 128, required: true };
const avatarOptions = [["", "默认头像"], ["🌱", "🌱 绿芽"], ["📚", "📚 书本"], ["🚲", "🚲 单车"]];

function loginPage(returnPath) {
  const error = errorBox();
  const submit = button("登录", "primary", null, { type: "submit" });
  const form = node("form", { class: "stack" },
    field("手机号", "phone", phoneInput),
    field("密码", "password", { type: "password", autocomplete: "current-password", required: true }),
    error, submit);
  bindForm(form, submit, error, async (data) => {
    const payload = await api("/api/auth/login", {
      phone: formString(data, "phone"), password: formString(data, "password")
    });
    state.account = payload.account;
    const admin = ["admin", "super"].includes(state.account.role);
    const destination = /^\/product\/\d+$/.test(returnPath || "") ? returnPath : "/";
    go(state.account.must_change ? "/first-password" : admin ? "/governance" : destination);
  });
  return authCard("手机号登录", "登录后可以发布商品和管理预约。", form,
    node("div", { class: "row" }, "还没有账号？", pathLink("注册", "/register")));
}

function registerPage() {
  const error = errorBox();
  const codeMessage = node("p", { class: "notice", role: "status", hidden: true });
  const sendCode = button("获取验证码", "secondary");
  const submit = button("注册账号", "primary", null, { type: "submit" });
  const phoneField = field("手机号", "phone", phoneInput);
  const form = node("form", { class: "stack" }, phoneField,
    node("div", { class: "verify-row" },
      field("短信验证码", "code", { required: true, maxlength: 12, autocomplete: "one-time-code", inputmode: "numeric" }), sendCode),
    codeMessage,
    node("div", { class: "columns" },
      field("昵称", "nickname", { required: true, maxlength: 32, autocomplete: "nickname" }),
      field("登录密码", "password", newPasswordInput, "至少 8 个字符。")),
    node("details", {}, node("summary", {}, "头像与个人简介（选填）"),
      node("div", { class: "columns" }, pick("头像", "avatar", avatarOptions), field("个人简介", "bio", { maxlength: 300 }))),
    error, submit);
  sendCode.addEventListener("click", busyButton(sendCode, error, async () => {
    const phone = phoneField.querySelector("input");
    if (!phone.reportValidity()) return;
    const response = await api("/api/auth/code", { phone: phone.value });
    codeMessage.textContent = response.development_code
      ? `本地短信模拟验证码：${response.development_code}` : (response.message || "已发送验证码。");
    codeMessage.hidden = false;
  }));
  bindForm(form, submit, error, async (data) => {
    await api("/api/auth/register", {
      phone: formString(data, "phone"), code: formString(data, "code"),
      nickname: formString(data, "nickname"), password: formString(data, "password"),
      bio: formString(data, "bio"), avatar: formString(data, "avatar")
    });
    go("/login", "注册成功，请登录。");
  });
  return authCard("手机号注册", "填写验证码后即可创建普通账号。", form,
    node("div", { class: "row" }, "已有账号？", pathLink("返回登录", "/login")), true);
}

function passwordForm(first) {
  const error = errorBox();
  const submit = button(first ? "保存并进入后台" : "修改密码", "primary", null, { type: "submit" });
  const form = node("form", { class: "stack" },
    field(first ? "初始密码" : "当前密码", "old_password", { type: "password", required: true, autocomplete: "current-password" }),
    field("新密码", "new_password", newPasswordInput, "至少 8 个字符。"),
    field("确认新密码", "confirm_password", newPasswordInput),
    error, submit);
  bindForm(form, submit, error, async (data) => {
    const newPassword = formString(data, "new_password");
    if (newPassword !== formString(data, "confirm_password")) throw new Error("两次输入的新密码不一致。");
    await api("/api/auth/password", { old_password: formString(data, "old_password"), new_password: newPassword });
    const session = await api("/api/me");
    state.account = session.account;
    if (!state.account) go("/login", "密码已修改，请用新密码登录。");
    else go(first ? "/governance" : "/profile", "密码已修改。");
  });
  return form;
}

function profilePage() {
  const user = state.account;
  const error = errorBox();
  const submit = button("保存资料", "primary", null, { type: "submit" });
  const options = [...avatarOptions];
  if (user.avatar && !options.some(([value]) => value === user.avatar)) {
    options.push([user.avatar, "当前头像"]);
  }
  const form = node("form", { class: "stack" },
    field("手机号", "phone", { value: user.phone, readonly: true }),
    field("昵称", "nickname", { required: true, maxlength: 32, value: user.nickname }),
    pick("头像", "avatar", options, user.avatar),
    area("个人简介", "bio", user.bio, { maxlength: 300 }), error, submit);
  bindForm(form, submit, error, async (data) => {
    await api("/api/auth/profile", {
      nickname: formString(data, "nickname"), bio: formString(data, "bio"), avatar: formString(data, "avatar")
    });
    state.account = (await api("/api/me")).account;
    go("/profile", "资料已保存。");
  });
  return pageContent(heading("个人账号", roleLabels[user.role]), node("div", { class: "columns" },
    panel("个人资料", form), panel("账号安全", passwordForm(false))));
}

function categoryOptions(first = "全部分类") {
  return [["", first], ...state.categories.map((item) => [String(item.id), item.name])];
}

function categoryName(id) {
  return state.categories.find((item) => Number(item.id) === Number(id))?.name || "未分类";
}

function productUrl(id) { return `/api/products/${segment(id)}`; }
function productPath(id) { return `/product/${segment(id)}`; }
function yuan(cents) { return cents == null || cents === "" ? "" : (Number(cents) / 100).toFixed(2).replace(/\.00$/, ""); }

function imageNode(photo, title) {
  if (!photo || !/^\d+$/.test(String(photo.id))) return node("div", {
    class: "product-image placeholder", "aria-label": "暂无实物图"
  }, "暂无实物图");
  return node("img", { class: "product-image", src: `/api/photos/${segment(photo.id)}`, alt: title, loading: "lazy" });
}

function productCard(product) {
  return node("article", { class: "product" },
    node("a", { href: `#${productPath(product.id)}` },
      imageNode(product.photos?.[0], product.title),
      node("div", { class: "product-body" },
        node("p", { class: "price" }, money(product.price_cents)),
        node("h3", {}, product.title),
        node("p", { class: "muted" }, `${categoryName(product.category_id)} · ${product.location || "交易地点待定"}`))));
}

async function marketPage(params) {
  const query = { ...params, scope: "public" };
  const result = await api(withQuery("/api/products", query));
  const error = errorBox();
  const submit = button("搜索", "primary", null, { type: "submit" });
  const form = node("form", { class: "filter-grid" },
    field("关键词", "q", { value: params.q, placeholder: "搜索商品标题或描述", maxlength: 100 }),
    pick("分类", "category_id", categoryOptions(), params.category_id),
    field("最低价（元）", "minimum", { inputmode: "decimal", value: yuan(params.minimum), placeholder: "不限" }),
    field("最高价（元）", "maximum", { inputmode: "decimal", value: yuan(params.maximum), placeholder: "不限" }),
    pick("排序", "sort", [["newest", "最新发布"], ["oldest", "最早发布"], ["price_asc", "价格从低到高"],
      ["price_desc", "价格从高到低"]], params.sort || "newest"),
    submit, node("div", { class: "wide" }, error));
  bindForm(form, submit, error, async (data) => {
    const minimum = asCents(formString(data, "minimum"), "最低价");
    const maximum = asCents(formString(data, "maximum"), "最高价");
    if (minimum !== "" && maximum !== "" && minimum > maximum) throw new Error("价格下限不能大于上限。");
    go(withQuery("/", {
      q: formString(data, "q").trim(), category_id: formString(data, "category_id"),
      minimum, maximum, sort: formString(data, "sort")
    }));
  });
  const publish = state.account?.role === "member" ? pathLink("＋ 发布商品", "/selling/new", "primary") : null;
  return pageContent(heading("二手商品", "浏览校园闲置，约定线下交接。", publish), panel("", form),
    node("div", { class: "row" },
      node("span", { class: "badge" }, "公开状态：已发布"),
      node("span", { class: "muted" }, `共 ${numberFormat.format(result.total)} 件商品`),
      Object.keys(params).some((key) => key !== "page") ? pathLink("清空筛选", "/") : null),
    result.items.length ? node("section", { class: "market-grid", "aria-label": "商品列表" }, result.items.map(productCard))
      : panel("", empty("暂无符合条件的商品，可以调整分类或价格。")),
    pager(result, (page) => withQuery("/", { ...params, page })));
}

function details(entries) {
  return node("dl", { class: "details-list" },
    entries.flatMap(([label, value]) => [node("dt", {}, label), node("dd", {}, value || "—")]));
}

function productSummary(product) {
  return details([
    ["分类", categoryName(product.category_id)], ["成色", product.condition],
    ["交易地点", product.location], ["卖家", product.seller_name || (product.seller_id ? `用户 ${product.seller_id}` : "")],
    ["价格", money(product.price_cents)]
  ]);
}

function productDecisionNotes(product) {
  const review = product.last_review;
  const violation = product.last_violation;
  return [
    review?.reason ? node("p", { class: review.status === "rejected" ? "error" : "notice" },
      `最近审核：${review.reason}`) : null,
    violation?.reason ? node("p", { class: "error" }, `违规处置原因：${violation.reason}`) : null,
    product.status === "violation" && review?.kind === "appeal" && review.status === "pending"
      ? node("p", { class: "notice" }, "整改申诉正在审核。") : null
  ];
}

function reportProduct(id, title, returnPath) {
  actionDialog({
    title: "举报商品", detail: node("p", {}, title),
    confirm: "提交举报", reason: "举报原因", reasonRequired: true,
    run: async (reason) => {
      await api(`${productUrl(id)}/report`, { reason });
      go(returnPath, "举报已提交。");
    }
  });
}

async function productPage(id) {
  const product = await api(productUrl(id));
  const user = state.account;
  const owner = user && Number(user.id) === Number(product.seller_id);
  const open = product.status === "published";
  const reserve = () => actionDialog({
    title: "发起预约",
    detail: node("p", {}, `${product.title} · ${money(product.price_cents)}。提交后等待卖家确认。`),
    confirm: "确认预约",
    run: async (_, key) => {
      try {
        await api(`${productUrl(id)}/reserve`, { version: product.version, request_key: key });
      } catch (error) {
        if (error instanceof HttpError && error.status === 409) {
          throw new Error(`${error.message} 请返回并刷新商品详情。`);
        }
        throw error;
      }
      go("/reservations", "预约已提交，等待卖家确认。");
    }
  });
  const report = () => reportProduct(id, product.title, productPath(id));
  const photos = product.photos || [];
  const controls = open && !owner && user?.role === "member"
    ? actions(button("发起预约", "primary", reserve), button("举报商品", "", report),
      button("刷新详情", "", () => render()))
    : open && !user ? actions(pathLink("登录后预约", withQuery("/login", { return: productPath(id) }), "primary"))
      : product.status === "sold" && !owner && user?.role === "member" ? actions(button("举报商品", "", report))
        : owner ? actions(pathLink("我的商品", "/selling", "secondary")) : null;
  return pageContent(heading(product.title, "", pathLink("返回集市", "/", "secondary")),
    node("div", { class: "split" },
      panel("", node("div", { class: "gallery" }, photos.length ? photos.map((photo) => imageNode(photo, product.title)) : imageNode(null, product.title))),
      panel("", node("div", { class: "stack" },
        actions(labelStatus(product.status), node("strong", { class: "price" }, money(product.price_cents))),
        productSummary(product),
        product.contact ? details([["卖家联系方式", product.contact]]) : null,
        node("p", { class: "description" }, product.description || "暂无商品描述。"),
        productDecisionNotes(product),
        product.status === "sold" ? node("p", { class: "notice" }, product.hidden
          ? "违规处置已关闭公开详情，原成交记录继续保留。"
          : "商品已售出。公开详情在售出后 30 天内保留。")
          : open ? node("p", { class: "muted" }, "预约表示购买意向。请线下验货、交接和付款。") : null,
        owner && open ? node("p", { class: "muted" }, "这是你发布的商品。") : null,
        controls))));
}

function productMutation(product, operation) {
  const spec = {
    submit: ["提交审核", "提交后等待管理员审核。", false],
    downlist: ["下架商品", "下架后，商品将从公开列表移除。", true],
    appeal: ["提交整改申诉", "说明整改情况，管理员复核通过后商品才会恢复发布。", false]
  }[operation];
  actionDialog({
    title: spec[0], detail: node("p", {}, `${product.title} · ${spec[1]}`), confirm: spec[0],
    danger: spec[2], reason: operation === "appeal" ? "整改与申诉说明" : "", reasonRequired: operation === "appeal",
    run: async (reason) => {
      const body = { version: product.version };
      if (operation === "appeal") body.reason = reason;
      await api(`${productUrl(product.id)}/${operation}`, body);
      go(currentRouteLink(), `${spec[0]}成功。`);
    }
  });
}

function currentRouteLink() { return location.hash.slice(1) || "/"; }

function sellerActions(product) {
  const items = [pathLink("详情", productPath(product.id))];
  const reviewingAppeal = product.status === "violation"
    && product.last_review?.kind === "appeal" && product.last_review.status === "pending";
  if (["draft", "rejected", "downlisted", "published", "violation"].includes(product.status) && !reviewingAppeal) {
    items.push(pathLink("编辑", `/selling/${segment(product.id)}/edit`, "secondary"));
  }
  if (["draft", "rejected", "downlisted"].includes(product.status)) {
    items.push(button("送审", "primary", () => productMutation(product, "submit")));
  }
  if (product.status === "published") items.push(button("下架", "danger", () => productMutation(product, "downlist")));
  if (product.status === "violation") items.push(reviewingAppeal
    ? node("span", { class: "muted" }, "申诉审核中") : button("整改申诉", "", () => productMutation(product, "appeal")));
  if (product.status === "reserved") items.push(pathLink("查看预约", "/reservations?scope=seller"));
  return actions(items);
}

async function sellingPage(params) {
  const result = await api(withQuery("/api/products", { ...params, scope: "mine" }));
  const error = errorBox();
  const submit = button("筛选", "primary", null, { type: "submit" });
  const form = node("form", { class: "row" },
    field("关键词", "q", { value: params.q, placeholder: "搜索我的商品", maxlength: 100 }),
    pick("商品状态", "status", [["", "全部状态"], ...Object.entries(productStatuses)], params.status),
    submit, pathLink("重置", "/selling"), error);
  bindForm(form, submit, error, async (data) => go(withQuery("/selling", {
    q: formString(data, "q").trim(), status: formString(data, "status")
  })));
  return pageContent(heading("我的商品", "管理草稿、审核结果和已发布商品。", pathLink("＋ 发布商品", "/selling/new", "primary")),
    panel("", form), panel("", table(["商品", "状态", "价格", "更新时间", "操作"], result.items.map((product) => [
      product.title, labelStatus(product.status), money(product.price_cents), time(product.updated_at), sellerActions(product)
    ]), "暂无商品，可以先保存草稿。")),
    pager(result, (page) => withQuery("/selling", { ...params, page })));
}

async function serializePhotos(files) {
  const entries = Array.from(files).filter((file) => file.size > 0);
  if (entries.length > 9) throw new Error("最多选择 9 张实物图。");
  if (entries.some((file) => !["image/jpeg", "image/png"].includes(file.type) || file.size > 5 * 1024 * 1024)) {
    throw new Error("实物图须为 JPG 或 PNG，每张不超过 5 MB。");
  }
  const photos = [];
  for (const file of entries) {
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => resolve(String(reader.result).split(",", 2)[1]));
      reader.addEventListener("error", () => reject(new Error("实物图读取失败，请重新选择。")));
      reader.readAsDataURL(file);
    });
    photos.push({ mime: file.type, data });
  }
  return photos;
}

async function productEditor(id = null) {
  const product = id === null ? null : await api(productUrl(id));
  if (product && (Number(product.seller_id) !== Number(state.account.id)
      || !["draft", "rejected", "downlisted", "published", "violation"].includes(product.status)
      || product.last_review?.status === "pending")) {
    throw new Error("当前商品状态或账号权限不允许编辑。");
  }
  const error = errorBox();
  const submit = button(product ? "保存修改" : "保存草稿", "primary", null, { type: "submit" });
  const form = node("form", { class: "stack" },
    field("商品标题", "title", { value: product?.title, required: true, maxlength: 50 }),
    node("div", { class: "columns" },
      pick("分类", "category_id", categoryOptions("请选择分类"), product?.category_id),
      field("价格（元）", "price", { inputmode: "decimal", value: yuan(product?.price_cents), placeholder: "1 至 10000 元" })),
    area("商品描述", "description", product?.description, { maxlength: 500 }),
    node("div", { class: "columns" },
      field("交易地点", "location", { value: product?.location, maxlength: 500 }),
      field("成色（选填）", "condition", { value: product?.condition, maxlength: 500 })),
    field("联系方式（选填）", "contact", { value: product?.contact, maxlength: 500 }),
    field("实物图", "photos", { type: "file", accept: "image/jpeg,image/png", multiple: true },
      "送审需要 1 至 9 张 JPG 或 PNG 实物图，每张不超过 5 MB。重新选图将替换已有实物图。"),
    product?.photos?.length ? node("label", { class: "row" }, node("input", { type: "checkbox", name: "clear_photos" }), "清除已有实物图") : null,
    error, actions(pathLink("返回", "/selling", "secondary"), submit));
  bindForm(form, submit, error, async (data) => {
    const price = asCents(formString(data, "price"), "价格");
    if (price !== "" && (price < 100 || price > 1000000)) throw new Error("价格须在 1 至 10000 元之间。");
    const payload = Object.fromEntries(["title", "description", "location", "condition", "contact"]
      .map((key) => [key, formString(data, key).trim()]));
    payload.price_cents = price === "" ? null : price;
    const category = formString(data, "category_id");
    payload.category_id = category ? Number(category) : null;
    const photos = await serializePhotos(data.getAll("photos"));
    if (photos.length && data.has("clear_photos")) throw new Error("请在替换或清除已有实物图中选择一种操作。");
    if (photos.length || data.has("clear_photos")) payload.photos = photos;
    if (product) payload.version = product.version;
    const saved = await api(product ? `${productUrl(id)}/edit` : "/api/products", payload);
    go("/selling", `已保存：${productStatuses[saved.status] || "请查看商品状态"}。`);
  });
  return pageContent(heading(product ? "编辑商品" : "发布商品",
    product?.status === "published" ? "修改核心资料或分类需重审。单独更新交易地点仅留痕。"
      : product?.status === "violation" ? "保存整改后，可在商品列表提交申诉。"
        : "只填写标题即可保存草稿，补齐资料后再送审。"),
    product ? productDecisionNotes(product) : null,
    node("div", { class: product?.photos?.length ? "split" : "" },
      panel("商品资料", form),
      product?.photos?.length ? panel("已有实物图", node("div", { class: "gallery" },
        product.photos.map((photo) => imageNode(photo, product.title)))) : null));
}

const reservationOperations = {
  confirm: { title: "确认预约", detail: "确认后商品继续占用，线下交接后可标记完成。" },
  reject: { title: "拒绝预约", detail: "拒绝后商品恢复可预约状态。", danger: true, reason: "拒绝原因（选填）" },
  cancel: { title: "取消预约", detail: "取消后商品恢复可预约状态，原预约保留在历史中。", danger: true },
  complete: { title: "标记交易完成", detail: "请在完成线下验货、付款和交接后确认，商品将同步标为已售出。" }
};

function reservationMutation(reservation, operation) {
  const spec = reservationOperations[operation];
  const confirmedCancel = operation === "cancel" && reservation.status === "confirmed";
  actionDialog({
    title: confirmedCancel ? "取消已确认预约" : spec.title, confirm: spec.title, danger: spec.danger,
    detail: node("div", { class: "stack" },
      node("p", {}, `${reservation.product_title} · #${reservation.id} · ${money(reservation.price_cents)}`),
      node("p", {}, spec.detail)),
    reason: confirmedCancel ? "取消原因（必填）" : spec.reason, reasonRequired: confirmedCancel,
    run: async (reason, key) => {
      await api(`/api/reservations/${segment(reservation.id)}/${operation}`, { request_key: key, reason });
      go(currentRouteLink(), `${spec.title}成功。`);
    }
  });
}

function reservationActions(reservation, scope) {
  const seller = scope === "seller";
  let permitted = [];
  if (reservation.status === "pending") permitted = seller ? ["confirm", "reject"] : ["cancel"];
  if (reservation.status === "confirmed") permitted = seller ? ["complete", "cancel"] : ["complete"];
  const controls = permitted.map((operation) => {
    const spec = reservationOperations[operation];
    return button(spec.title, spec.danger ? "danger" : "primary", () => reservationMutation(reservation, operation));
  });
  if (!seller) controls.push(button("举报商品", "", () => {
    reportProduct(reservation.product_id, reservation.product_title, currentRouteLink());
  }));
  return controls.length ? actions(controls) : "—";
}

async function reservationsPage(params, scope = null) {
  scope = scope || (params.scope === "seller" ? "seller" : "buyer");
  const admin = scope === "admin";
  const path = admin ? "/governance" : "/reservations";
  const fixed = admin ? { tab: "reservations" } : { scope };
  const result = await api(withQuery("/api/reservations", { status: params.status, page: params.page, scope }));
  const error = errorBox();
  const submit = button("筛选", "primary", null, { type: "submit" });
  const form = node("form", { class: "row" },
    pick("预约状态", "status", [["", "全部状态"], ...Object.entries(reservationStatuses)], params.status),
    submit, pathLink("刷新", withQuery(path, { ...fixed, status: params.status })), error);
  bindForm(form, submit, error, async (data) => {
    go(withQuery(path, { ...fixed, status: formString(data, "status") }));
  });
  const cells = result.items.map((item) => [
    node("div", {}, node("strong", {}, item.product_title), node("p", { class: "muted" }, `预约 #${item.id}`)),
    money(item.price_cents),
    labelStatus(item.status, reservationStatuses),
    node("div", {}, node("p", {}, time(item.created_at)),
      item.confirmed_at ? node("p", { class: "muted" }, `确认：${time(item.confirmed_at)}`) : null,
      item.ended_at ? node("p", { class: "muted" }, `结束：${time(item.ended_at)}`) : null),
    admin ? `买家 #${item.buyer_id} / 卖家 #${item.seller_id}`
      : item.reason || (item.status === "pending" ? "待处理满 24 小时自动取消"
        : item.status === "confirmed" ? (scope === "buyer" ? "如需取消请线下与卖家协商" : "确认满 10 天未完成时自动处理") : "—"),
    admin ? pathLink("商品详情", productPath(item.product_id)) : reservationActions(item, scope)
  ]);
  return pageContent(
    admin ? governanceHeader("reservations") : heading("预约交易", "查看本人发起的预约，或处理本人商品收到的预约。"),
    !admin ? chooseLinks([
      ["buyer", "我发起的预约", "/reservations?scope=buyer"],
      ["seller", "收到的预约", "/reservations?scope=seller"]
    ], scope) : null,
    panel("", form), panel("", table(["商品", "预约价格", "状态", "时间", admin ? "交易双方" : "说明", "操作"], cells)),
    pager(result, (page) => withQuery(path, { ...fixed, status: params.status, page })));
}

function governanceHeader(selected) {
  return node("div", { class: "stack" },
    heading("审核与违规治理", "审核待发布商品，处理举报和整改申诉。"),
    chooseLinks([
      ["reviews", "商品审核与整改", "/governance?tab=reviews"],
      ["reports", "用户举报", "/governance?tab=reports"],
      ["products", "平台商品", "/governance?tab=products"],
      ["reservations", "平台预约", "/governance?tab=reservations"]
    ], selected));
}

function adminProductPreview(product, context = "") {
  return node("div", { class: "stack" },
    node("p", {}, product.title), context ? node("p", { class: "quiet" }, context) : null,
    productSummary(product),
    product.description ? node("p", { class: "description" }, product.description) : null,
    product.photos?.length ? node("div", { class: "gallery" }, product.photos.map((photo) => imageNode(photo, product.title))) : null);
}

async function openModeration(control, error, entry, kind, decision) {
  await busyButton(control, error, async () => {
    const product = await api(productUrl(entry.product_id ?? entry.id));
    const review = kind === "reviews";
    const direct = kind === "products";
    const dangerous = decision === "reject" || decision === "violate";
    const title = direct || decision === "violate" ? "确认违规下架"
      : decision === "dismiss" ? "驳回举报" : decision === "approve" ? "通过审核" : "驳回审核";
    const context = direct ? "如有未结束的预约，下架时同步取消。"
      : review && entry.kind === "appeal" ? `整改申诉：${entry.reason || entry.request_reason || "已提交整改说明"}`
        : !review ? `举报原因：${entry.reason || "—"}` : "";
    actionDialog({
      title, confirm: title, danger: dangerous,
      detail: adminProductPreview(product, context), reason: "处理说明", reasonRequired: dangerous,
      run: async (reason) => {
        const path = direct ? `/api/admin/products/${segment(product.id)}/violate`
          : `/api/admin/${kind}/${segment(entry.id)}`;
        await api(path, direct ? { reason } : { decision, reason });
        go(currentRouteLink(), `${title}成功。`);
      }
    });
  })();
}

function moderationButton(label, kind, error, item, section, decision) {
  const control = button(label, kind);
  control.addEventListener("click", () => openModeration(control, error, item, section, decision));
  return control;
}

const reviewStatus = { pending: "待处理", approved: "已通过", rejected: "已驳回", closed: "已关闭" };
const reportStatus = { pending: "待处理", dismissed: "已驳回", violation: "已确认违规" };

async function moderationQueue(tab, params) {
  const review = tab === "reviews";
  const query = { status: params.status || "pending", kind: review ? params.kind : "" };
  const result = await api(withQuery(`/api/admin/${tab}`, query));
  const error = errorBox();
  const submit = button("筛选", "primary", null, { type: "submit" });
  const labels = review ? reviewStatus : reportStatus;
  const form = node("form", { class: "row" },
    pick("处理状态", "status", [["all", "全部"], ...Object.entries(labels)], query.status),
    review ? pick("审核类型", "kind", [["all", "全部"], ["submission", "商品送审"], ["appeal", "整改申诉"]], params.kind || "all") : null,
    submit, button("刷新", "secondary", () => go(currentRouteLink())));
  bindForm(form, submit, error, async (data) => go(withQuery("/governance", {
    tab, status: formString(data, "status"), kind: formString(data, "kind")
  })));
  const rows = result.items.map((item) => [
    `#${item.id}`,
    pathLink(item.title, productPath(item.product_id)),
    review ? (item.kind === "appeal" ? "整改申诉" : "商品送审") : item.reason,
    labelStatus(item.status, labels), time(item.created_at),
    item.status === "pending" ? actions(
      moderationButton(review ? "通过" : "确认违规", review ? "primary" : "danger", error, item, tab, review ? "approve" : "violate"),
      moderationButton(review ? "驳回" : "驳回举报", review ? "danger" : "", error, item, tab, review ? "reject" : "dismiss"))
      : (item.decision_reason || item.result_reason || item.reason || "—")
  ]);
  return pageContent(governanceHeader(tab), panel("", form), error,
    panel("", table(["编号", "商品", review ? "类型" : "举报原因", "状态", "提交时间", "处理"], rows,
      review ? "当前没有符合条件的审核记录。" : "当前没有符合条件的举报。")));
}

async function adminProductsPage(params) {
  const result = await api(withQuery("/api/products", {
    scope: "admin", status: params.status, q: params.q, page: params.page
  }));
  const error = errorBox();
  const submit = button("筛选", "primary", null, { type: "submit" });
  const form = node("form", { class: "row" },
    field("关键词", "q", { value: params.q, placeholder: "商品标题或描述" }),
    pick("商品状态", "status", [["", "全部状态"], ...Object.entries(productStatuses)], params.status), submit);
  bindForm(form, submit, error, async (data) => go(withQuery("/governance", {
    tab: "products", status: formString(data, "status"), q: formString(data, "q").trim()
  })));
  return pageContent(governanceHeader("products"), panel("", form), error,
    panel("", table(["商品", "发布者", "状态", "价格", "处置"], result.items.map((product) => [
      pathLink(product.title, productPath(product.id)), product.seller_name || `#${product.seller_id}`,
      labelStatus(product.status), money(product.price_cents),
      product.status === "violation" || product.hidden ? "已处置"
        : moderationButton("违规下架", "danger", error, product, "products", "violate")
    ]))), pager(result, (page) => withQuery("/governance", { ...params, tab: "products", page })));
}

async function adminAccountsPage() {
  const result = await api("/api/admin/accounts");
  const error = errorBox();
  const submit = button("创建管理员", "primary", null, { type: "submit" });
  const form = node("form", { class: "stack" },
    field("登录手机号", "phone", phoneInput), field("昵称", "nickname", { required: true, maxlength: 32 }),
    field("初始密码", "password", newPasswordInput, "至少 8 个字符。新账号首次登录后须改密。"), error, submit);
  bindForm(form, submit, error, async (data) => {
    await api("/api/admin/accounts", Object.fromEntries(["phone", "nickname", "password"]
      .map((key) => [key, formString(data, key)])));
    go("/admin-accounts", "管理员已创建。");
  });
  return pageContent(heading("管理员账号", "超级管理员可以创建管理员账号。"),
    node("div", { class: "split" },
      panel("已有账号", table(["手机号", "昵称", "权限", "状态"], result.items.map((account) => [
        account.phone, account.nickname, roleLabels[account.role] || account.role,
        node("span", { class: account.must_change ? "badge warning" : "badge" }, account.must_change ? "待首次改密" : "正常")
      ]))), panel("创建管理员", form)));
}

function metric(title, value) {
  return node("article", { class: "metric" }, node("p", {}, title), node("strong", {}, numberFormat.format(value)));
}

function dailyBars(days) {
  const top = Math.max(1, ...days.flatMap((day) => [day.reservations, day.completed]));
  return node("div", { class: "daily-bars" }, days.map((day) => node("div", { class: "daily-row" },
    node("strong", {}, day.date),
    node("div", { class: "bars" }, ["reservations", "completed"].map((key) => {
      const value = Number(day[key]);
      const label = key === "reservations" ? "预约" : "完成";
      return node("div", { class: "bar-row" },
        node("span", {}, label),
        node("progress", { class: `bar ${key}`, max: top, value, "aria-label": `${day.date} ${label}` }),
        node("strong", {}, value));
    })))));
}

async function statsPage(admin) {
  const result = await api(`/api/stats?scope=${admin ? "admin" : "mine"}`);
  if (!admin) return pageContent(heading("卖家统计", "汇总本人商品的发布、预约和成交记录。"),
    node("div", { class: "metric-grid" }, metric("曾发布商品", result.published),
      metric("已完成交易", result.completed), metric("成功创建的预约", result.reservations)));
  return pageContent(heading("平台业务统计", "按北京时间汇总最近 7 天的交易数据。"),
    node("div", { class: "metric-grid" }, metric("全平台商品", result.total_products),
      metric("近 7 天预约", result.daily.reduce((total, day) => total + Number(day.reservations), 0)),
      metric("近 7 天完成", result.daily.reduce((total, day) => total + Number(day.completed), 0))),
    node("div", { class: "columns" },
      panel("近 7 天预约与成交", dailyBars(result.daily)),
      panel("违规事件", table(["卖家", "次数"], result.violations.map((row) => [
        `${row.nickname}（#${row.account_id}）`, row.count
      ]), "暂无已确认违规记录。"))),
    node("div", { class: "columns" },
      panel("按分类", table(["分类", "商品数"], result.categories.map((row) => [row.name, row.count]))),
      panel("按状态", table(["状态", "商品数"], result.statuses.map((row) => [labelStatus(row.status), row.count])))));
}

const logActions = {
  register: "注册", login: "登录", logout: "退出", change_password: "修改密码",
  update_profile: "修改资料", create_admin: "创建管理员", bootstrap_super: "初始化超级管理员",
  "product.create": "新建草稿", "product.edit": "编辑商品", "product.submit": "商品送审",
  "product.downlist": "主动下架", "product.violate": "违规下架", "review.approve": "通过审核",
  "review.reject": "驳回审核", "report.create": "提交举报", "report.dismiss": "驳回举报",
  "report.violate": "确认举报违规", "appeal.submit": "提交整改申诉",
  "reservation.create": "发起预约", "reservation.confirm": "确认预约",
  "reservation.reject": "拒绝预约", "reservation.cancel": "取消预约",
  "reservation.complete": "完成交易", "reservation.expire": "预约超时"
};
const objectLabels = {
  account: "账号", product: "商品", product_review: "审核", product_report: "举报",
  reservation: "预约", violation: "违规记录", notification: "通知"
};

function stateText(value) { return productStatuses[value] || reservationStatuses[value] || value || "—"; }

async function logsPage(params, admin) {
  const scope = admin ? "admin" : "mine";
  const result = await api(withQuery("/api/logs", { ...params, scope }));
  const error = errorBox();
  const submit = button("查询", "primary", null, { type: "submit" });
  const form = node("form", { class: "filter-grid" },
    field("开始日期", "start", { type: "date", value: params.start }),
    field("结束日期", "end", { type: "date", value: params.end }),
    admin ? field("操作者编号", "actor_id", { type: "number", min: 1, step: 1, value: params.actor_id }) : null,
    pick("操作类型", "action", [["", "全部类型"], ...Object.entries(logActions)], params.action),
    field("商品编号", "product_id", { type: "number", min: 1, step: 1, value: params.product_id }),
    submit, node("div", { class: "wide" }, error));
  bindForm(form, submit, error, async (data) => {
    const start = formString(data, "start");
    const end = formString(data, "end");
    if (start && end && start > end) throw new Error("开始日期不能晚于结束日期。");
    go(withQuery("/logs", Object.fromEntries([...data.entries()].filter(([, value]) => value))));
  });
  const labels = ["时间", ...(admin ? ["操作者"] : []), "操作", "对象", "状态变化", "结果", "说明", ...(admin ? ["IP"] : [])];
  const rows = result.items.map((item) => [
    time(item.created_at),
    ...(admin ? [item.actor_id ? `#${item.actor_id}` : "系统或匿名"] : []),
    logActions[item.action] || item.action,
    item.object_id ? `${objectLabels[item.object_type] || item.object_type || "记录"} #${item.object_id}` : "—",
    item.before || item.after ? `${stateText(item.before)} → ${stateText(item.after)}` : "—",
    node("span", { class: item.success ? "badge" : "badge danger" }, item.success ? "成功" : "失败"),
    item.detail || "—",
    ...(admin ? [item.ip || "—"] : [])
  ]);
  return pageContent(heading(admin ? "平台操作日志" : "个人操作历史",
    admin ? "按北京时间和操作者查询平台业务记录。" : "这里仅展示与你本人有关的操作。"),
    panel("", form), panel("", table(labels, rows)),
    pager(result, (page) => withQuery("/logs", { ...params, page })));
}

async function notificationsPage(params) {
  const result = await api("/api/notifications");
  const unread = result.items.filter((item) => !item.read);
  const selected = params.tab === "unread" ? "unread" : "all";
  const items = selected === "unread" ? unread : result.items;
  return pageContent(heading("站内通知", `当前有 ${unread.length} 条未读通知。`),
    chooseLinks([["all", "全部", "/notifications"], ["unread", "未读", "/notifications?tab=unread"]], selected),
    items.length ? node("div", { class: "stack" }, items.map((item) => {
      const error = errorBox();
      const mark = button("标为已读");
      mark.addEventListener("click", busyButton(mark, error, async () => {
        await api(`/api/notifications/${segment(item.id)}/read`, {});
        go(currentRouteLink(), "通知已标为已读。");
      }));
      return panel("", node("article", { class: "stack" },
        actions(node("span", { class: item.read ? "badge neutral" : "badge" }, item.read ? "已读" : "未读"),
          node("time", { class: "muted" }, time(item.created_at))),
        node("p", { class: "description" }, item.message),
        item.read ? null : actions(mark), error));
    })) : panel("", empty("暂无通知。")));
}

function accessPage(message, loggedIn = Boolean(state.account)) {
  return pageContent(heading("访问提示"), panel("", node("div", { class: "stack" },
    node("p", { class: "error", role: "alert" }, message),
    actions(loggedIn ? pathLink("返回集市", "/", "secondary") : pathLink("登录", "/login", "primary")))));
}

async function renderRoute(route) {
  const { path, params } = route;
  const user = state.account;
  const admin = ["admin", "super"].includes(user?.role);
  const member = user?.role === "member";
  const product = path.match(/^\/product\/(\d+)$/);
  const edit = path.match(/^\/selling\/(\d+)\/edit$/);
  if (path === "/") return marketPage(params);
  if (product) return productPage(product[1]);
  if (path === "/login") return loginPage(params.return);
  if (path === "/register") return registerPage();
  if (!user) return accessPage("请登录后使用此功能。", false);
  if (path === "/first-password" && user.must_change) {
    return authCard("修改初始密码", "首次登录后须修改初始密码，之后进入后台。", passwordForm(true));
  }
  if (path === "/profile" || path === "/first-password") return profilePage();
  if (path === "/notifications") return notificationsPage(params);
  if (path === "/stats") return statsPage(admin);
  if (path === "/logs") return logsPage(params, admin);
  if (path === "/selling" || path === "/selling/new" || edit || path === "/reservations") {
    if (!member) return accessPage("当前账号不能使用普通用户交易功能。");
    if (path === "/selling") return sellingPage(params);
    if (path === "/selling/new" || edit) return productEditor(edit ? edit[1] : null);
    return reservationsPage(params);
  }
  if (path === "/admin-accounts") {
    return user.role === "super" ? adminAccountsPage() : accessPage("仅超级管理员可以管理管理员账号。");
  }
  if (path === "/governance") {
    if (!admin) return accessPage("仅管理员可以访问审核与治理。");
    if (params.tab === "products") return adminProductsPage(params);
    if (params.tab === "reservations") return reservationsPage(params, "admin");
    return moderationQueue(params.tab === "reports" ? "reports" : "reviews", params);
  }
  return accessPage("找不到该页面。");
}

async function render() {
  const route = currentRoute();
  if (state.account?.must_change && route.path !== "/first-password") {
    location.replace("#/first-password");
    return;
  }
  const renderId = ++state.renderId;
  const flash = state.flash;
  state.flash = "";
  renderNavigation(route);
  app.setAttribute("aria-busy", "true");
  app.replaceChildren(node("p", { class: "quiet" }, "正在加载…"));
  try {
    const body = await renderRoute(route);
    if (renderId !== state.renderId) return;
    app.replaceChildren(...(flash ? [node("p", { class: "notice", role: "status" }, flash), body] : [body]));
    if (flash) tell(flash);
    const title = body.querySelector("h1")?.textContent;
    document.title = title ? `${title} · 环校集市` : "环校集市 · 校园二手交易";
  } catch (error) {
    if (renderId !== state.renderId) return;
    if (error instanceof HttpError && error.status === 401) {
      state.account = null;
      renderNavigation(route);
      app.replaceChildren(accessPage(error.message, false));
    } else {
      app.replaceChildren(pageContent(heading("暂时无法打开页面"),
        panel("", node("div", { class: "stack" }, node("p", { class: "error", role: "alert" }, error.message),
          actions(button("重试", "primary", () => render()), pathLink("返回集市", "/", "secondary"))))));
    }
  } finally {
    if (renderId === state.renderId) app.removeAttribute("aria-busy");
  }
}

async function boot() {
  try {
    const payload = await api("/api/me");
    state.account = payload.account;
    state.categories = payload.categories || [];
    await render();
  } catch (error) {
    app.replaceChildren(pageContent(heading("环校集市"),
      panel("", node("div", { class: "stack" }, node("p", { class: "error", role: "alert" }, error.message),
        actions(button("重新连接", "primary", () => boot()))))));
  }
}

window.addEventListener("hashchange", () => render());
document.querySelector(".skip-link").addEventListener("click", (event) => {
  event.preventDefault();
  document.querySelector("#content").focus();
});
boot();
