import path from "node:path";

import { expect, test } from "@playwright/test";

import { corpusRoot } from "./paths";

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
