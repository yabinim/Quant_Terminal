import time
from pyngrok import ngrok

# 1. 대표님의 통행증(Authtoken) 등록
# [중요] 아래 큰따옴표 안에 대표님의 토큰 번호를 붙여넣으세요.
token = "3ACQn9nA1Ih7G1DpcsoaIaEwizo_32298PXZVUmDJCjL1S3fc" 
ngrok.set_auth_token(token)

try:
    # 2. 8501 포트(주식 앱)로 터널 연결
    print("🚀 외부 통로를 개방하는 중입니다...")
    public_url = ngrok.connect(8501).public_url
    
    print("\n" + "="*50)
    print(f"✅ 외부 접속 주소가 생성되었습니다!")
    print(f"🔗 주소: {public_url}")
    print("="*50)
    print("\n💡 이 주소를 복사해서 Bitly(비틀리) Target에 넣으세요.")
    print("⚠️ 주의: 이 창을 끄면 밖에서 접속이 안 됩니다!")

    # 터널 유지를 위해 무한 대기
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("\n🛑 터널 연결을 종료합니다.")
    ngrok.kill()