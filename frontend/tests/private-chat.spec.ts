import { expect, test } from "@playwright/test";
import { disconnect, emit, eventTransport, streamCount } from "./repository-fixtures";
import {
  activeSha, baseId, chatRoutes, citationId, conversation, conversationId, history,
  jobId, jobView, secondConversationId, targetSha,
} from "./chat-fixtures";

test.beforeEach(async ({ page }) => {
  await eventTransport(page);
});

test("test_scope_versions_citations_and_gap", async ({ page }) => {
  const current = await chatRoutes(page);
  await page.goto(`/conversations/${conversationId}`);

  const scope = page.getByRole("region", { name: "会话范围" });
  await expect(scope).toContainText("目标 SHA");
  await expect(scope).toContainText(targetSha);
  await expect(scope).toContainText("旧 active SHA");
  await expect(scope).toContainText(activeSha);

  const messages = page.getByRole("region", { name: "历史消息" });
  await expect(messages).toContainText("上一轮的结论，引用了服务端证据。");
  // The round's frozen version is shown from the stored citation, not client input.
  await expect(messages).toContainText(`SHA ${targetSha.slice(0, 12)}`);
  await messages.getByRole("button", { name: /docs\/base\.md/ }).click();
  const previewRegion = page.getByRole("region", { name: "引用预览" });
  await expect(previewRegion).toContainText("服务端返回的引用正文");
  await expect(previewRegion).toContainText("第 12–14 行");
  // React StrictMode may repeat the immutable preview request.
  expect(current.citations.length).toBeGreaterThan(0);
  expect(current.citations.every(path => path.endsWith(`/citations/${citationId}`))).toBe(true);

  await scope.getByRole("radio", { name: "旧 active 版本" }).check();
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("新的一轮问题");
  await page.getByRole("button", { name: "提交问题" }).click();
  await expect.poll(() => current.submitted.length).toBe(1);
  expect(current.submitted[0].body).toEqual({
    selection: {
      selected_repository_ids: [baseId],
      indexes: [{ repository_id: baseId, sha: activeSha, target: "active" }],
    },
    question: "新的一轮问题",
  });
  expect(current.submitted[0].key).toBeTruthy();

  // The run ends with no answer message: the UI reports a local gap instead of
  // inventing text, and the receipt stops looking like an active run.
  await expect.poll(() => streamCount(page)).toBeGreaterThan(0);
  current.jobs.set(jobId, jobView("succeeded"));
  await emit(page, 2, "running");
  await emit(page, 3, "succeeded");
  await expect(page.getByText("本轮没有找到可用证据，未发送云请求；可以调整仓库范围后重新提问。")).toBeVisible();
  await expect(page.getByRole("region", { name: "回答进度" })).toHaveCount(0);
});

test("test_cancel_disconnect_and_late_response", async ({ page }) => {
  const current = await chatRoutes(page);
  current.conversations.push(conversation(secondConversationId, [baseId]));
  current.messages.set(secondConversationId, []);

  await page.goto(`/conversations/${conversationId}`);
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("需要取消的问题");
  await page.getByRole("button", { name: "提交问题" }).click();
  await expect.poll(() => current.submitted.length).toBe(1);

  await expect.poll(() => streamCount(page)).toBeGreaterThan(0);
  current.jobs.set(jobId, jobView("running"));
  await emit(page, 2, "running");
  const progress = page.getByRole("region", { name: "回答进度" });
  await expect(progress).toContainText("正在生成回答");
  await disconnect(page);
  await expect(progress).toContainText("连接暂时中断，正在恢复…");

  await page.getByRole("button", { name: "取消本次回答" }).click();
  await expect.poll(() => current.cancels).toEqual([jobId]);
  // The receipt is released and the terminal Job is never shown as active.
  await expect(page.getByRole("region", { name: "回答进度" })).toHaveCount(0);

  // A late history response for the old conversation cannot populate another.
  let release: () => void = () => {};
  current.gate = { promise: new Promise<void>(resolve => { release = resolve; }), release: () => release() };
  await page.goto(`/conversations/${conversationId}`);
  await page.getByRole("textbox", { name: "问题", exact: true }).fill("第一个会话的问题");
  await page.getByRole("button", { name: "提交问题" }).click();
  await page.getByRole("link", { name: "← 返回会话列表" }).click();
  await page.locator(`a[href="/conversations/${secondConversationId}"]`).click();
  current.gate.release();
  await expect(page.getByRole("region", { name: "历史消息" })).toContainText("还没有消息。");
  await expect(page.getByText("第一个会话的问题")).toHaveCount(0);
  expect(current.messages.get(conversationId)?.some(message => message.body === "第一个会话的问题")).toBe(true);
});

test("test_revocation_clears_private_history", async ({ page }) => {
  const current = await chatRoutes(page);
  await page.goto(`/conversations/${conversationId}`);
  await expect(page.getByRole("region", { name: "历史消息" })).toContainText("上一轮的结论");

  current.denied.add(conversationId);
  await page.reload();
  await expect(page.getByText("仓库权限已撤销，历史、引用、进度和答案均已停止加载。")).toBeVisible();
  await expect(page.getByText("上一轮的结论")).toHaveCount(0);
  await expect(page.getByText("上一轮的问题")).toHaveCount(0);
  expect(current.messages.get(conversationId)).toEqual(history());
});
