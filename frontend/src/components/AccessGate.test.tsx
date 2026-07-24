import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  accessConfiguration,
  clearAccessKey,
  getStoredAccessKey,
  onUnauthorized,
  storeAccessKey,
  verifyAccessKey
} from "../api";
import { AccessGate } from "./AccessGate";


vi.mock("../api", () => ({
  accessConfiguration: vi.fn(),
  clearAccessKey: vi.fn(),
  getStoredAccessKey: vi.fn(() => ""),
  onUnauthorized: vi.fn(() => () => undefined),
  storeAccessKey: vi.fn(),
  verifyAccessKey: vi.fn()
}));

const mockedConfiguration = vi.mocked(accessConfiguration);
const mockedVerify = vi.mocked(verifyAccessKey);
const mockedStore = vi.mocked(storeAccessKey);

describe("AccessGate", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(clearAccessKey).mockImplementation(() => undefined);
    vi.mocked(getStoredAccessKey).mockReturnValue("");
    vi.mocked(onUnauthorized).mockReturnValue(() => undefined);
  });

  afterEach(cleanup);

  it("enters internal deployments without asking for a key", async () => {
    mockedConfiguration.mockResolvedValue({ accessKeyRequired: false });

    render(
      <AccessGate>
        <div>workspace ready</div>
      </AccessGate>
    );

    expect(await screen.findByText("workspace ready")).not.toBeNull();
    expect(mockedVerify).not.toHaveBeenCalled();
  });

  it("rejects a wrong external key and stores only a verified key", async () => {
    const user = userEvent.setup();
    mockedConfiguration.mockResolvedValue({ accessKeyRequired: true });
    mockedVerify.mockResolvedValueOnce(false).mockResolvedValueOnce(true);

    render(
      <AccessGate>
        <div>workspace ready</div>
      </AccessGate>
    );

    const input = await screen.findByLabelText("访问密钥");
    await user.type(input, "wrong-key");
    await user.click(screen.getByRole("button", { name: "进入工作台" }));
    expect((await screen.findByRole("alert")).textContent).toContain(
      "访问密钥不正确"
    );
    expect(mockedStore).not.toHaveBeenCalled();
    expect(screen.queryByText("workspace ready")).toBeNull();

    await user.clear(input);
    await user.type(input, "correct-access-key");
    await user.click(screen.getByRole("button", { name: "进入工作台" }));

    await waitFor(() => expect(screen.getByText("workspace ready")).not.toBeNull());
    expect(mockedStore).toHaveBeenCalledWith("correct-access-key");
    expect(screen.getByText("外部访问已验证")).not.toBeNull();
  });
});
