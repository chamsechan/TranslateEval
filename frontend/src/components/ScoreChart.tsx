import { useEffect, useRef } from 'react'
import type { ThresholdSummary } from '../types'

export default function ScoreChart({ data }: { data: ThresholdSummary['by_language'] }) {
  const ref = useRef<HTMLDivElement>(null)
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
        renderers.CanvasRenderer,
      ])
      const instance = echarts.init(element)
      chart = instance
      instance.setOption({
        color: ['#246bfd', '#38b98f'],
        tooltip: { trigger: 'axis' },
        legend: { top: 0, data: ['平均分', '阈值准确率'] },
        grid: { left: 48, right: 28, top: 48, bottom: 48 },
        xAxis: { type: 'category', data: data.map((item) => item.source_language), axisLabel: { rotate: data.length > 12 ? 35 : 0 } },
        yAxis: [
          { type: 'value', name: '分数' },
          { type: 'value', name: '准确率', min: 0, max: 1, axisLabel: { formatter: (value: number) => `${Math.round(value * 100)}%` } },
        ],
        series: [
          { name: '平均分', type: 'bar', data: data.map((item) => item.mean == null ? null : Number(item.mean.toFixed(3))), barMaxWidth: 24, itemStyle: { borderRadius: [5, 5, 0, 0] } },
          { name: '阈值准确率', type: 'line', yAxisIndex: 1, smooth: true, data: data.map((item) => item.accuracy), symbolSize: 7 },
        ],
      })
      observer = new ResizeObserver(() => instance.resize())
      observer.observe(element)
    })
    return () => { disposed = true; observer?.disconnect(); chart?.dispose() }
  }, [data])
  return <div ref={ref} className="chart-box" />
}
