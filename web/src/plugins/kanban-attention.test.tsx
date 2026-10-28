// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import { exposePluginSDK, getPluginComponent } from "./registry";

const tasks = [
  { id: "final", title: "Release review", status: "review", current_attention: {
    kind: "awaiting-test", run_id: 1, badge: { label: "Final PR", tone: "success" }
  } },
  { id: "test", title: "Testing review", status: "review", current_attention: { kind: "awaiting-test", run_id: 2 } },
  { id: "answer", title: "Question review", status: "review", current_attention: { kind: "awaiting-answer", run_id: 3 } },
  { id: "failure", title: "Failed run", status: "ready", current_attention: { kind: "run-failed", run_id: 4 } },
  { id: "stale", title: "Already running", status: "running", current_attention: {
    kind: "awaiting-test", run_id: 5, badge: { label: "Old approval", tone: "success" }
  } },
  { id: "escaped", title: "Other producer", status: "review", current_attention: {
    kind: "awaiting-test", run_id: 6, badge: { label: "<img src=x onerror=alert(1)>", tone: "info" }
  } },
];

beforeAll(async () => {
  exposePluginSDK();
  const sdk = window.__HERMES_PLUGIN_SDK__!;
  sdk.useI18n = () => ({ t: { kanban: null } } as never);
  sdk.fetchJSON = vi.fn(async (url: string) => {
    const path = url.split("?")[0];
    if (path.endsWith("/board")) return {
      columns: ["review", "ready", "running"].map(name => ({ name, tasks: tasks.filter(t => t.status === name) })),
      assignees: [], tenants: [], latest_event_id: 0,
    };
    if (path.endsWith("/boards")) return { boards: [], current: "" };
    if (path.includes("/tasks/")) return { task: tasks.find(t => path.endsWith("/" + t.id)), runs: [], comments: [], events: [], attachments: [] };
    return {};
  }) as typeof sdk.fetchJSON;
  sdk.buildWsUrl = () => new Promise(() => {});
  // @ts-expect-error The served plugin is a plain JavaScript IIFE, not a typed module.
  await import("../../../plugins/kanban/dashboard/dist/index.js");
});

afterEach(cleanup);

it("renders the served plugin's colored card badges and the same single badge in detail", async () => {
  const Page = getPluginComponent("kanban")!;
  const { container } = render(<Page />);
  await screen.findByText("Release review");
  const card = screen.getByText("Release review").closest(".hermes-kanban-card") as HTMLElement;
  expect(within(card).getAllByText("Final PR")).toHaveLength(1);
  expect(within(card).queryByText("Awaiting your testing")).toBeNull();
  expect(screen.getByText("Final PR").className).toContain("attention--success");
  expect(screen.getByText("Awaiting your testing").className).toContain("attention--info");
  expect(screen.getByText("Needs your answer").className).toContain("attention--warning");
  expect(screen.getByText("Run failed").className).toContain("attention--danger");
  expect(screen.queryByText("Old approval")).toBeNull();
  expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeTruthy();
  expect(container.querySelector("img")).toBeNull();
  fireEvent.click(screen.getByText("Release review"));
  const detail = await screen.findByText("Status");
  const drawer = detail.closest(".hermes-kanban-drawer-body") as HTMLElement;
  expect(within(drawer).getAllByText("Final PR")).toHaveLength(1);
  expect(within(drawer).queryByText("Awaiting your testing")).toBeNull();
});
