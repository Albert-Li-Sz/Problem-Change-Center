import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

const root = path.resolve(import.meta.dirname, "../..");
const python = process.env.P2H_E2E_PYTHON ?? path.join(root, "backend/.venv/bin/python");
const runtime = path.join(root, ".public-dev/browser");

test("invite, verify email, convert in Docker, download, history, logout", async ({ page }) => {
  const seed = JSON.parse(fs.readFileSync(path.join(runtime, "seed.json"), "utf8"));
  await page.goto("/");
  await page.getByRole("button", { name: "使用邀请码注册", exact: true }).click();
  await page.getByLabel("邮箱", { exact: true }).fill("browser@example.com");
  await page.locator('input[name="password"]').fill("browser-test-password-2026");
  await page.getByLabel("邀请码", { exact: true }).fill(seed.invitation);
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "加入公益站", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("验证邮件");
  const mailPath = fs.readdirSync(path.join(seed.data_dir, "dev-mail")).find((name) => name.endsWith("-verify.txt"))!;
  const link = fs.readFileSync(path.join(seed.data_dir, "dev-mail", mailPath), "utf8");
  await page.goto(link);
  await page.reload();
  await page.getByRole("button", { name: "验证邮箱", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("验证成功");
  await page.getByLabel("邮箱", { exact: true }).fill("browser@example.com");
  await page.getByLabel("密码", { exact: true }).fill("browser-test-password-2026");
  await page.getByRole("button", { name: "欢迎回来", exact: true }).click();
  await expect(page.getByRole("button", { name: "任务历史", exact: true })).toBeVisible();
  const corpus = path.join(runtime, "corpus");
  execFileSync(python, [path.join(root, "compat/corpus/build.py"), corpus]);
  const fixture = "hydro-core-rich.zip";
  await page.locator('input[type="file"]').first().setInputFiles(path.join(corpus, fixture));
  await page.getByRole("button", { name: "上传并检查", exact: true }).click();
  await expect(page.getByRole("button", { name: "启动容器转换", exact: true })).toBeEnabled({ timeout: 60_000 });
  await page.getByRole("button", { name: "启动容器转换", exact: true }).click();
  const downloadButton = page.getByRole("button", { name: /下载结果/ });
  await expect(downloadButton).toBeEnabled({ timeout: 60_000 });
  const download = page.waitForEvent("download");
  await downloadButton.click();
  expect((await download).suggestedFilename()).toContain("oj-package-");
  await page.getByRole("button", { name: "任务历史", exact: true }).click();
  await expect(page.locator(".public-job")).toHaveCount(1);
  await expect(page.locator(".public-job")).toContainText("已完成");
  await page.screenshot({ path: path.join(runtime, "history.png"), fullPage: true });
  await page.getByRole("button", { name: "查看 / 继续", exact: true }).click();
  await expect(downloadButton).toBeEnabled({ timeout: 15_000 });
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
});

test("administrator invitations and queue control", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("邮箱", { exact: true }).fill("admin@example.com");
  await page.getByLabel("密码", { exact: true }).fill("browser-test-password-2026");
  await page.getByRole("button", { name: "欢迎回来", exact: true }).click();
  await page.getByRole("button", { name: "管理后台", exact: true }).click();
  await page.getByRole("button", { name: "生成 10 个一次性邀请码" }).click();
  await expect(page.getByLabel("生成的邀请码")).not.toHaveValue("");
  await page.getByRole("button", { name: "暂停领取任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "恢复任务队列", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "恢复任务队列", exact: true }).click();
  await page.screenshot({ path: path.join(runtime, "admin.png"), fullPage: true });
});

test("mobile registration layout", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: path.join(runtime, "mobile.png"), fullPage: true });
});
