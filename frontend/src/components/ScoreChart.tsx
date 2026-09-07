import { useEffect, useMemo, useRef, useState } from 'react'
import { Select, Space, Typography } from 'antd'
import { languagePairLabel } from '../languages'
import type { ThresholdSummary } from '../types'

export default function ScoreChart({ data }: { data: ThresholdSummary['by_language'] }) {
  const ref = useRef<HTMLDivElement>(null)
  const [languages, setLanguages] = useState<string[]>([])
  const [sort, setSort] = useState<'language' | 'accuracy' | 'mean'>('language')
  const visible = useMemo(() => data.filter(item => !languages.length || languages.includes(item.source_language)).sort((a, b) => sort === 'language' ? a.source_language.localeCompare(b.source_language) : (a[sort] ?? Infinity) - (b[sort] ?? Infinity)), [data, languages, sort])
  useEffect(() => {
    if (!ref.current) return
    const element = ref.current
    let disposed = false
    let observer: ResizeObserver | undefined
    let chart: { resize: () => void; dispose: () => void } | undefined
    void Promise.all([
      import('echarts/core'),
      import('echarts/charts'),
      import('echarts/components'),
      import('echarts/renderers'),
    ]).then(([echarts, charts, components, renderers]) => {
      if (disposed) return
      echarts.use([
        charts.BarChart,
        charts.LineChart,
        components.GridComponent,
        components.TooltipComponent,
        components.LegendComponent,
        components.DataZoomComponent,
        renderers.CanvasRenderer,
      ])
      const instance = echarts.init(element)
      chart = instance
      instance.setOption({
        color: ['#246bfd', '#38b98f'],
        tooltip: { trigger: 'axis' },
        legend: { top: 0, data: ['平均分', '阈值通过率'] },
        grid: { left: 48, right: 28, top: 48, bottom: 90 },
        xAxis: { type: 'category', data: visible.map((item) => item.source_language), axisLabel: { rotate: visible.length > 12 ? 35 : 0, interval: 0 } },
        dataZoom: [{ type: 'slider', xAxisIndex: 0, bottom: 10, height: 24, startValue: 0, endValue: Math.max(0, Math.min(19, visible.length - 1)) }, { type: 'inside', xAxisIndex: 0 }],
        yAxis: [
          { type: 'value', name: '分数' },
          { type: 'value', name: '通过率', min: 0, max: 1, axisLabel: { formatter: (value: number) => `${Math.round(value * 100)}%` } },
        ],
        series: [
          { name: '平均分', type: 'bar', data: visible.map((item) => item.mean == null ? null : Number(item.mean.toFixed(3))), barMaxWidth: 24, itemStyle: { borderRadius: [5, 5, 0, 0] } },
          { name: '阈值通过率', type: 'line', yAxisIndex: 1, smooth: true, data: visible.map((item) => item.accuracy), symbolSize: 7 },
        ],
      })
      observer = new ResizeObserver(() => instance.resize())
      observer.observe(element)
    })
    return () => { disposed = true; observer?.disconnect(); chart?.dispose() }
  }, [visible])
  return <>
    <Space wrap style={{ marginBottom: 12 }}>
      <Select aria-label="图表语种" mode="multiple" showSearch optionFilterProp="label" allowClear placeholder="全部语种，可搜索并选择" maxTagCount="responsive" style={{ width: 360, maxWidth: '100%' }} value={languages} onChange={setLanguages} options={data.map(item => ({ value: item.source_language, label: languagePairLabel(item.source_language) }))} />
      <Select aria-label="图表排序" style={{ width: 190 }} value={sort} onChange={setSort} options={[{ value: 'language', label: '按语种代码' }, { value: 'accuracy', label: '通过率从低到高' }, { value: 'mean', label: '平均分从低到高' }]} />
    </Space>
    <Typography.Paragraph type="secondary">显示 {visible.length} 个语种；语种较多时先显示 20 项，可拖动图下滑块查看其余语种。</Typography.Paragraph>
    <div ref={ref} className="chart-box" />
  </>
}
