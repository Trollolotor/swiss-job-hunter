import {useEffect, useLayoutEffect, useRef, useState} from "react";

const WIDTH_KEY = "sjh.detailWidth";
const DEFAULT_WIDTH = 400;

function savedWidth() {
  try {
    const value = Number(window.localStorage.getItem(WIDTH_KEY));
    return Number.isFinite(value) && value > 0 ? value : DEFAULT_WIDTH;
  } catch {
    return DEFAULT_WIDTH;
  }
}

export default function DetailPane({children}) {
  const pane = useRef(null);
  const drag = useRef(null);
  const [requestedWidth, setRequestedWidth] = useState(savedWidth);
  const [available, setAvailable] = useState(Math.max(0, window.innerWidth - 300));
  // Keep room for both panes, relaxing their minimums on small windows.
  const min = Math.min(320, available / 2);
  const max = Math.max(min, available - Math.min(420, available / 2));
  const clamp = value => Math.round(Math.max(min, Math.min(value, max)));
  const width = clamp(requestedWidth);

  useLayoutEffect(() => {
    const measure = () => {
      const node = pane.current;
      const list = node?.previousElementSibling;
      if (!node || !list) return;
      const space = node.parentElement.getBoundingClientRect().right - list.getBoundingClientRect().left;
      setAvailable(space > 0 ? space : Math.max(0, window.innerWidth - 300));
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(pane.current.parentElement);
    window.addEventListener("resize", measure);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); };
  }, []);

  useEffect(() => {
    try { window.localStorage.setItem(WIDTH_KEY, String(requestedWidth)); } catch { /* private mode */ }
  }, [requestedWidth]);

  useEffect(() => {
    const move = event => {
      if (drag.current) setRequestedWidth(clamp(drag.current.right - event.clientX));
    };
    const finish = () => { drag.current = null; };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    window.addEventListener("blur", finish);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      window.removeEventListener("blur", finish);
    };
  }, [min, max]);

  return <div ref={pane} aria-label="Vacancy details" style={{width, minWidth:0, position:"relative",
    borderLeft:"1px solid #d4dece", display:"flex", flexDirection:"column", background:"#f0f3ed", flexShrink:0}}>
    <div role="separator" tabIndex={0} aria-label="Resize vacancy details" aria-orientation="vertical"
      aria-valuemin={Math.round(min)} aria-valuemax={Math.round(max)} aria-valuenow={width}
      onPointerDown={event=>{
        if (event.button !== 0) return;
        event.preventDefault();
        drag.current = {right:pane.current.getBoundingClientRect().right};
        event.currentTarget.setPointerCapture?.(event.pointerId);
      }}
      onKeyDown={event=>{
        const next = {ArrowLeft:width+20, ArrowRight:width-20, Home:min, End:max}[event.key];
        if (next === undefined) return;
        event.preventDefault(); setRequestedWidth(clamp(next));
      }}
      style={{position:"absolute", left:-4, top:0, bottom:0, width:8, zIndex:2,
        cursor:"col-resize", touchAction:"none", userSelect:"none"}}/>
    {children}
  </div>;
}
