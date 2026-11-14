import { test, expect } from "playwright/test";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

test("real dashboard shows communication history, links, redaction and distinct messages", async ({
  page,
}) => {
  await page.coverage.startJSCoverage({ resetOnNavigation: false });
  await page.goto("/#/communications");
  await expect(
    page.getByRole("heading", { name: "Agent communications", exact: true }),
  ).toBeVisible();
  await expect(page.locator("[data-communication]")).toHaveCount(50);
  const first = page.locator("[data-communication]").first();
  await expect(
    first.getByRole("link", { name: "Claude", exact: true }),
  ).toHaveAttribute("href", "#/herd/session/sender");
  await expect(
    first.getByRole("link", { name: "Codex", exact: true }),
  ).toHaveAttribute("href", "#/herd/session/recipient");
  await expect(first).toContainText("Delivered");
  await expect(first).toContainText("Host observation");
  await expect(first.locator("pre")).toContainText("<script>");
  await expect(first).not.toContainText("browsersecretvalue");
  expect(
    await page.evaluate(() => Object.hasOwn(window, "auditInjected")),
  ).toBe(false);
  const delivered = await page.request.post("/api/v1/ingest", {
    data: {
      source: "herdr-cli",
      entity_type: "communication",
      entity_id: "browser_live",
      event_type: "communication.delivered",
      payload: {
        operation_id: "browser_live",
        status: "delivered",
        record_id: "browser_live_delivered",
        reported_at: Date.now() / 1000,
        text: "New live communication",
        truncated: false,
        host_id: "browser-test",
        sender: { pane_id: "w1:p1" },
        recipient: { pane_id: "w1:p2" },
        route: [],
        sender_origin: "caller_environment",
        channel: "herdr.agent.prompt",
        evidence_kind: "host_observation",
      },
    },
  });
  expect(delivered.ok()).toBe(true);
  await expect(
    page.getByText("New live communication", { exact: true }),
  ).toBeVisible({ timeout: 10000 });
  await page.getByRole("button", { name: "Older messages" }).click();
  await expect(page.locator("[data-communication]")).toHaveCount(62);
  const ids = await page
    .locator("[data-communication]")
    .evaluateAll((rows) =>
      rows.map((row) => row.getAttribute("data-communication")),
    );
  expect(new Set(ids).size).toBe(62);
  await page.getByRole("button", { name: "Latest", exact: true }).click();
  await expect(page.locator("[data-communication]")).toHaveCount(50);
  await page.screenshot({
    path: ".superpowers/communications-dashboard.png",
    fullPage: false,
  });
  const coverage = await page.coverage.stopJSCoverage();
  const entry = coverage.find((item) =>
    item.url.endsWith("/communications-view.js"),
  );
  expect(entry).toBeDefined();
  await fs.mkdir(".superpowers/communications-v8", { recursive: true });
  if (entry)
    await fs.writeFile(
      ".superpowers/communications-v8/browser.json",
      JSON.stringify({
        result: [
          {
            url: pathToFileURL(path.resolve("packages/communications-view.js"))
              .href,
            functions: entry.functions,
          },
        ],
      }),
    );
});
