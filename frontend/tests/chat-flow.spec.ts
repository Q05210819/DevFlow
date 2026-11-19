import { expect, test } from "@playwright/test";
import { emit, eventTransport, streamCount } from "./repository-fixtures";
import {
  answerMessageId, baseId, chatRoutes, citation, conversationId, jobId, jobView, state, targetSha,
} from "./chat-fixtures";

test.beforeEach(async ({ page }) => {
  await eventTransport(page);
});

test("create, stream, read the answer and delete a private conversation", async ({ page }) => {
  const current = state();
  current.conversations = [];
  current.messages.clear();
  await chatRoutes(page, current);

  await page.goto("/conversations");
  const create = page.getByRole("region", { name: "新建会话" });
  await create.getByRole("checkbox", { name: "选择仓库 team/base-app", exact: true }).check();
  await create.getByRole("button", { name: "创建会话" }).click();
  await expect(page).toHaveURL(new RegExp(`/conversations/${conversationId}$`));
  expect(current.creates).toHaveLength(1);
  expect(current.creates[0].body).toEqual({ selected_repository_ids: [baseId] });
  expect(current.creates[0].key).toBeTruthy();

  const scope = page.getByRole("region", { name: "会话范围" });
  await expect(scope).toContainText(targetSha);

  await page.getByRole("textbox", { name: "问题", exact: true }).fill("这个仓库如何部署？");
  await page.getByRole("button", { name: "提交问题" }).click();
  await expect.poll(() => current.submitted.length).toBe(1);

  await expect.poll(() => streamCount(page)).toBeGreaterThan(0);
  current.jobs.set(jobId, jobView("running"));
  await emit(page, 2, "running");
  await expect(page.getByRole("region", { name: "回答进度" })).toContainText("正在生成回答");

  const answered = [
    {
      id: answerMessageId, role: "assistant" as const, body: "结论：使用容器镜像部署。",
      ordinal: 1, created_at: "2026-11-06T00:01:00Z", citations: [citation],
    },
    ...(current.messages.get(conversationId) ?? []),
  ];
  current.messages.set(conversationId, answered);
  current.jobs.set(jobId, jobView("succeeded"));
  await emit(page, 3, "succeeded");

  const history = page.getByRole("region", { name: "历史消息" });
  await expect(history).toContainText("结论：使用容器镜像部署。");
  await expect(history).toContainText("这个仓库如何部署？");
  await expect(page.getByRole("region", { name: "回答进度" })).toHaveCount(0);

  await history.getByRole("button", { name: /docs\/base\.md/ }).click();
  await expect(page.getByRole("region", { name: "引用预览" })).toContainText("服务端返回的引用正文");
  // React StrictMode may repeat the immutable preview request.
  expect(current.citations.length).toBeGreaterThan(0);

  const removal = page.getByRole("region", { name: "删除会话" });
  await removal.getByRole("button", { name: "删除会话" }).click();
  await removal.getByRole("button", { name: "确认删除会话" }).click();
  await expect(page).toHaveURL(/\/conversations$/);
  await expect.poll(() => current.deletions).toEqual([conversationId]);
  await expect(page.getByRole("region", { name: "会话列表" })).toContainText("还没有会话。选择仓库后创建。");
});
