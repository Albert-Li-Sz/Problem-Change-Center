import path from "node:path";

import { expect, test } from "@playwright/test";

import { corpusRoot } from "./paths";

test("opens and closes the capability matrix dialog", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/");

  const trigger = page.getByRole("button", { name: "查看格式能力矩阵" });
  const dialog = page.getByRole("dialog", { name: "格式能力矩阵" });
  const matrixScroller = dialog.locator(".capability-matrix-scroll");

  await trigger.click();
  await expect(dialog).toBeVisible();
  const dimensions = await matrixScroller.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
    parentWidth: element.parentElement?.clientWidth ?? 0
  }));
  expect(dimensions.clientWidth).toBeLessThanOrEqual(dimensions.parentWidth);
  expect(dimensions.scrollWidth).toBeGreaterThanOrEqual(dimensions.clientWidth);

  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();

  await trigger.click();
  await page.getByRole("button", { name: "关闭格式能力矩阵" }).click();
  await expect(dialog).not.toBeVisible();
});

test("uploads, converts over SSE, downloads the report result, and cleans up", async ({
  page
}) => {
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));

  await page.goto("/");
  await expect(page).toHaveTitle("OJ 题包转换器");
  await expect(page.getByRole("heading", { name: "OJ 题包转换器" })).toBeVisible();

  await page.locator('input[type="file"]').setInputFiles(
    path.resolve(corpusRoot, "hydro-core-rich.zip")
  );
  await page.getByRole("button", { name: "上传并检查" }).click();
  await expect(page.getByText("识别为 HydroOJ")).toBeVisible();
  await page.getByRole("combobox", { name: "输出格式" }).selectOption("icpc");
  await page.getByRole("button", { name: "启动容器转换" }).click();

  await expect(page.locator(".status")).toHaveText("成功");
  await expect(page.getByText("转换报告")).toBeVisible();
  await expect(page.getByText("1 题 · 0 警告 · 0 项损失")).toBeVisible();
  await expect(page.getByText("fake runner: conversion complete")).toBeVisible();
  expect(requests.some((url) => url.endsWith("/events"))).toBe(true);
  expect(requests.some((url) => url.endsWith("/logs"))).toBe(false);

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载结果" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(
    /^oj-package-convert-[0-9a-f]{32}\.zip$/
  );

  await page.getByRole("button", { name: "清理任务" }).click();
  await expect(page.getByText("hydro-core-rich.zip")).toHaveCount(0);
  await expect(page.locator(".status")).toHaveCount(0);
});

test("cancels a running conversion and keeps diagnostics until cleanup", async ({
  page
}) => {
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles(
    path.resolve(corpusRoot, "hydro-core-rich.zip")
  );
  await page.getByRole("button", { name: "上传并检查" }).click();
  await expect(page.getByText("识别为 HydroOJ")).toBeVisible();
  await page.getByRole("combobox", { name: "输出格式" }).selectOption("dmoj");
  await page.getByRole("button", { name: "启动容器转换" }).click();

  await expect(page.locator(".status")).toHaveText("转换中");
  await expect(page.getByText("fake runner: waiting for cancellation")).toBeVisible();
  await page.getByRole("button", { name: "取消并保留诊断" }).click();
  await expect(page.locator(".status")).toHaveText("已取消");
  await expect(page.getByText("Cancelled by user")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "取消并保留诊断" })
  ).toHaveCount(0);

  await page.getByRole("button", { name: "清理任务" }).click();
  await expect(page.locator(".status")).toHaveCount(0);
});

test("resolves an auto-detected ProbHub workspace before conversion", async ({
  page
}) => {
  let submittedSource: string | undefined;
  let submittedOnly: string[] | undefined;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().endsWith("/api/jobs")
    ) {
      const payload = request.postDataJSON();
      submittedSource = payload.source_format;
      submittedOnly = payload.only;
    }
  });

  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles(
    path.resolve(corpusRoot, "probhub-workspace.zip")
  );
  await page.getByRole("button", { name: "上传并检查" }).click();
  await expect(
    page.getByText("识别为 ProbHub Workspace / Legacy / DOMjudge")
  ).toBeVisible();
  await expect(page.getByText("多题包 · 2 题 · 工作区结构")).toBeVisible();
  await page.getByRole("combobox", { name: "转换范围" }).selectOption("selected");
  await expect(
    page.getByText("指定题目模式下至少选择一道题目。")
  ).toBeVisible();
  await page.getByRole("checkbox", { name: /L02/ }).check();
  await page.getByRole("button", { name: "启动容器转换" }).click();

  await expect(page.locator(".status")).toHaveText("成功");
  expect(submittedSource).toBe("auto");
  expect(submittedOnly).toEqual(["L02"]);
  await expect(page.getByText("probhub → hydro")).toBeVisible();

  await page.getByRole("button", { name: "清理任务" }).click();
  await expect(page.locator(".status")).toHaveCount(0);
});

test("imports an auto-detected ProbHub legacy problem directory", async ({
  page
}) => {
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles(
    path.resolve(corpusRoot, "probhub-legacy.zip")
  );
  await page.getByRole("button", { name: "上传并检查" }).click();
  await expect(
    page.getByText("识别为 ProbHub Workspace / Legacy / DOMjudge")
  ).toBeVisible();
  await page.getByRole("button", { name: "启动容器转换" }).click();

  await expect(page.locator(".status")).toHaveText("成功");
  await expect(page.getByText("probhub → hydro")).toBeVisible();
  await expect(page.getByText("转换报告")).toBeVisible();

  await page.getByRole("button", { name: "清理任务" }).click();
  await expect(page.locator(".status")).toHaveCount(0);
});
