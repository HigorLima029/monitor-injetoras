declare module '@novnc/novnc/lib/rfb' {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: { shared?: boolean; wsProtocols?: string[]; credentials?: { password?: string } })
    viewOnly: boolean
    scaleViewport: boolean
    resizeSession: boolean
    background: string
    disconnect(): void
    sendCredentials(credentials: { username?: string; password?: string; target?: string }): void
  }
}
