const displayNames = typeof Intl.DisplayNames === 'function'
  ? new Intl.DisplayNames(['zh-CN'], { type: 'language', fallback: 'none' })
  : null

/** Display helpers never change the source code used in filters or stored samples. */
export function languageName(code: string, suppliedName?: string | null): string {
  if (suppliedName && suppliedName !== code) return suppliedName
  try {
    return displayNames?.of(code.replaceAll('_', '-')) || code
  } catch {
    return code
  }
}

export function languageLabel(code: string, suppliedName?: string | null): string {
  const name = languageName(code, suppliedName)
  return name === code ? code : `${name}（${code}）`
}

export function languagePairLabel(code: string, suppliedName?: string | null): string {
  return `${languageLabel(code, suppliedName)} → 中文（zh）`
}
