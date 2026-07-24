import path from "node:path";

import { expect, test } from "@playwright/test";

import { corpusRoot } from "./paths";


const accessKey = "e2e-external-access-key";

test("protects the complete external conversion flow with a session access key", async ({
  page
}) => {
  const protectedRequests: Array<{ url: string; key: string | undefined }> = [];
  page.on("request", (request) => {
    if (/\/api\/(?:inspect|jobs)/.test(request.url())) {
      protectedRequests.push({
        url: request.url(),
        key: request.headers()["x-p2h-access-key"]
      });
    }
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "访问验证" })).toBeVisible();

  await page.getByLabel("访问密钥").fill("incorrect-external-key");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("alert")).toHaveText("访问密钥不正确。");
  await expect(page.getByRole("heading", { name: "OJ 题包转换器" })).toHaveCount(0);

  await page.getByLabel("访问密钥").fill(accessKey);
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.getByRole("heading", { name: "OJ 题包转换器" })).toBeVisible();
  await expect(page.getByText("外部访问已验证")).toBeVisible();

  const storage = await page.evaluate(() => ({
    session: sessionStorage.getItem("p2h.access-key"),
    local: localStorage.getItem("p2h.access-key")
  }));
  expect(storage).toEqual({ session: accessKey, local: null });
  expect(page.url()).not.toContain(accessKey);

  await page.locator('input[type="file"]').setInputFiles(
    path.resolve(corpusRoot, "hydro-core-rich.zip")
  );
  await page.getByRole("button", { name: "上传并检查" }).click();
  await expect(page.getByText("识别为 HydroOJ")).toBeVisible();
  await page.getByRole("combobox", { name: "输出格式" }).selectOption("icpc");
  await page.getByRole("button", { name: "启动容器转换" }).click();
  await expect(page.locator(".status")).toHaveText("成功");

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载结果" }).click();
  await downloadPromise;

  expect(protectedRequests.some(({ url }) => url.endsWith("/events"))).toBe(true);
  expect(protectedRequests.some(({ url }) => url.endsWith("/download"))).toBe(true);
  expect(protectedRequests).not.toHaveLength(0);
  for (const request of protectedRequests) {
    expect(request.key, request.url).toBe(accessKey);
    expect(request.url).not.toContain(accessKey);
  }

  await page.getByRole("button", { name: "锁定" }).click();
  await expect(page.getByRole("heading", { name: "访问验证" })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem("p2h.access-key"))).toBeNull();
});
