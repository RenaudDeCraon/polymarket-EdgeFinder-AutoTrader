# Polymarket BTC 5-Min Dutch Booking Bot

## Strateji
"Dutch Booking" — BTC Up/Down 5dk marketinde fiyat salınımı sırasında iki tarafı da ucuzken alarak, toplam maliyetin share başına <$1 olmasını sağla → garantili kâr.

## Kurulum

```bash
# 1. Repo'yu klonla / dosyaları indir
cd polymarket_bot

# 2. Virtual environment oluştur
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Bağımlılıkları yükle
pip install -r requirements.txt

# 4. .env dosyasını konfigüre et (trade mode için)
cp .env.example .env
# .env'yi düzenle ve private key'ini ekle
```

## Kullanım

### 1. Monitoring Mode (Tavsiye edilen başlangıç)
Sadece izler, trade yapmaz, fırsatları loglar:
```bash
python polymarket_btc_monitor.py --windows 5
```

### 2. API Test
Bağlantıyı ve market keşfini test et:
```bash
python polymarket_btc_monitor.py --test
```

### 3. Trade Mode (İLERİDE — henüz aktif değil)
```bash
python polymarket_btc_monitor.py --trade --windows 10
```

## Polymarket Cüzdan Kurulumu

Bot'un trade yapabilmesi için:
1. Polymarket'te MetaMask veya email ile giriş yap
2. Polygon ağında USDC'ye ihtiyacın var (Polymarket deposit ile yapabilirsin)
3. **Export Private Key**: MetaMask → Account Details → Export Private Key
4. `.env` dosyasına key'i yapıştır

⚠️ **Private key'ini kimseyle paylaşma, git'e commit etme!**

## Strateji Parametreleri

| Parametre | Default | Açıklama |
|-----------|---------|----------|
| MAX_ENTRY_PRICE | 0.30 | Bir tarafı almak için max fiyat (30¢) |
| MAX_TOTAL_COST | 0.92 | İki tarafın toplam max maliyeti (92¢) |
| BET_SIZE_USD | 2.00 | Her taraf için max bahis ($2) |
| MAX_TOTAL_RISK | 10.00 | Toplam risk limiti ($10) |

## Nasıl Çalışır

```
Window açılır (t=0)
  │
  ├─ BTC hedefin üstünde → Up pahalı (70-80¢), Down ucuz (20-30¢)
  │   └─ Down'ı al (ucuz taraf)
  │
  ├─ BTC döner, hedefe yaklaşır/altına düşer
  │   └─ Up ucuzlar (20-30¢)
  │       └─ Up'ı da al → DUTCH BOOK COMPLETE
  │
  ├─ Sonuç ne olursa olsun:
  │   ├─ Up kazanırsa → Up share'ler $1 öder
  │   └─ Down kazanırsa → Down share'ler $1 öder
  │   └─ Toplam maliyet < $1/share ise → KÂR
  │
Window kapanır (t=5dk)
```

## Riskler

- **Slippage**: Order'ın beklediğin fiyattan dolmayabilir
- **Likidite**: Thin market'lerde yeterli depth olmayabilir
- **Hız**: Profesyonel botlar ms'ler içinde hareket eder
- **Fırsat sıklığı**: Dutch book fırsatı nadir oluşabilir
- **API kesintisi**: Bağlantı kopabilir kritik anda
