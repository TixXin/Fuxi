import { useEffect, useRef } from "react";
import type { BridgeEvent } from "./api";

// App 只开一条服务端推送，收到的事件在页面之间转发；页面用 useBridgeEvent 订阅自己关心的种类。
const target = new EventTarget();

export function emit(e: BridgeEvent): void {
  target.dispatchEvent(new CustomEvent("event", { detail: e }));
}

export function useBridgeEvent(kinds: BridgeEvent["kind"][], handler: (e: BridgeEvent) => void): void {
  const ref = useRef(handler);
  ref.current = handler;
  const key = kinds.join(",");
  useEffect(() => {
    const wanted = new Set(key.split(","));
    const listener = (ev: Event) => {
      const e = (ev as CustomEvent<BridgeEvent>).detail;
      if (wanted.has(e.kind)) ref.current(e);
    };
    target.addEventListener("event", listener);
    return () => target.removeEventListener("event", listener);
  }, [key]);
}
