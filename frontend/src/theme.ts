import { useEffect, useState } from 'react'

export type ThemeChoice = 'auto' | 'light' | 'dark'

const KEYS = [
  'series-1', 'series-2', 'series-3', 'series-4', 'series-5', 'series-6', 'series-7',
  'ink', 'ink-2', 'ink-3', 'grid', 'rule', 'setpoint', 'band', 'panel', 'critical', 'good', 'warning',
] as const
export type Tokens = Record<(typeof KEYS)[number], string>

function read(): Tokens {
  const css = getComputedStyle(document.documentElement)
  return Object.fromEntries(KEYS.map((k) => [k, css.getPropertyValue(`--${k}`).trim()])) as Tokens
}

function savedTheme(): ThemeChoice {
  try {
    const v = localStorage.getItem('tema')
    return v === 'light' || v === 'dark' ? v : 'auto'
  } catch {
    return 'auto'
  }
}

/** Tema escolhido + cores resolvidas (o SVG dos gráficos precisa do hex, não da var CSS). */
export function useTheme() {
  const [choice, setChoice] = useState<ThemeChoice>(savedTheme)
  const [tokens, setTokens] = useState<Tokens>(() => read())

  useEffect(() => {
    const root = document.documentElement
    if (choice === 'auto') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', choice)
    try {
      localStorage.setItem('tema', choice)
    } catch {
      /* armazenamento indisponível: só não lembra a escolha */
    }
    setTokens(read())
  }, [choice])

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setTokens(read())
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return { choice, setChoice, tokens }
}
