#!/usr/bin/env python3
"""
事类检测器 HTTP API 服务
端口 8014

输入：起课引擎全量数据 + 事类名称
输出：证数报告
"""
import sys
import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

# 确保能导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shilei_detector import detect_shilei, detect_all_shilei

PORT = 8014


class ShileiHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/detect':
            self.handle_detect()
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path == '/health':
            self.send_json({'status': 'ok', 'port': PORT})
        else:
            self.send_error(404)

    def handle_detect(self):
        """处理检测请求"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)

            # 提取参数
            ke_data = data.get('ke_data', {})
            shilei_name = data.get('shilei', '')
            detail = data.get('detail', False)

            if not ke_data:
                self.send_json({
                    'success': False,
                    'error': '缺少 ke_data 参数'
                }, 400)
                return

            # 批量计算所有事类
            if shilei_name == 'all' or not shilei_name:
                result = detect_all_shilei(ke_data, detail)
                self.send_json(result)
                return

            # 单事类检测
            result = detect_shilei(ke_data, shilei_name, detail)
            self.send_json(result)

        except Exception as e:
            self.send_json({
                'success': False,
                'error': str(e)
            }, 500)

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        """自定义日志格式"""
        print(f"[{self.log_date_time_string()}] {args[0]}")


def main():
    print(f"🚀 事类检测器启动，端口 {PORT}")
    server = HTTPServer(('0.0.0.0', PORT), ShileiHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 服务已停止")
        server.server_close()


if __name__ == '__main__':
    main()
