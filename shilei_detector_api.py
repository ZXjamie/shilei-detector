#!/usr/bin/env python3
"""
事类检测器 HTTP API 服务（FastAPI 异步版）
端口 8014

输入：起课引擎全量数据 + 事类名称
输出：证数报告
"""
import sys
import os
import json
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# 确保能导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shilei_detector import detect_shilei, detect_all_shilei, detect_all_for_weight, load_weight_config

PORT = 8014

app = FastAPI(title="事类检测器", version="2.0.0")


@app.on_event("startup")
async def startup():
    print(f"🚀 事类检测器启动，端口 {PORT}")


@app.get("/health")
async def health():
    return {"status": "ok", "port": PORT}


@app.get("/reload")
async def reload():
    try:
        load_weight_config()
        return {"success": True, "message": "权重配置已重新加载"}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/detect")
async def detect(request: Request):
    try:
        data = await request.json()
        
        # 提取参数
        ke_data = data.get('ke_data', {})
        shilei_name = data.get('shilei', '')
        detail = data.get('detail', False)
        
        if not ke_data:
            return JSONResponse({
                'success': False,
                'error': '缺少 ke_data 参数'
            }, status_code=400)
        
        # CPU密集型，用 run_in_executor 避免阻塞
        loop = asyncio.get_event_loop()
        
        # 批量计算所有事类
        if shilei_name == 'all' or not shilei_name:
            result = await loop.run_in_executor(None, detect_all_shilei, ke_data, detail)
            return result
        
        # 单事类检测
        result = await loop.run_in_executor(None, detect_shilei, ke_data, shilei_name, detail)
        return result
        
    except Exception as e:
        return JSONResponse({
            'success': False,
            'error': str(e)
        }, status_code=500)


@app.post("/detect_for_weight")
async def detect_for_weight(request: Request):
    try:
        data = await request.json()
        ke_data = data.get('ke_data', {})
        if not ke_data:
            return JSONResponse({'success': False, 'error': '缺少 ke_data 参数'}, status_code=400)
        
        # CPU密集型，用 run_in_executor 避免阻塞
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, detect_all_for_weight, ke_data)
        return result
        
    except Exception as e:
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=PORT, log_level='info')
