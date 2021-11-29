import torch.nn as nn
import torch
import model.blocks as blocks
import torch.nn.functional as F

def make_model(args):
    return RECONS_VIDEO(in_channels=args.n_colors,
                        n_sequence=args.n_sequence,
                        out_channels=args.n_colors,
                        n_resblock=args.n_resblock,
                        n_feat=args.n_feat)

def pixel_unshuffle(input, downscale_factor):
    '''
    input: batchSize * c * k*w * k*h
    downscale_factor: k
    batchSize * c * k*w * k*h -> batchSize * k*k*c * w * h
    '''
    c = input.shape[1]
    kernel = torch.zeros(size = [downscale_factor * downscale_factor * c, 1, downscale_factor, downscale_factor],
                        device = input.device)
    for y in range(downscale_factor):
        for x in range(downscale_factor):
            kernel[x + y * downscale_factor::downscale_factor * downscale_factor, 0, y, x] = 1
    return F.conv2d(input, kernel, stride = downscale_factor, groups = c)

class Pixel_UnShuffle(nn.Module):
    def __init__(self, downscale_factor):
        super(Pixel_UnShuffle, self).__init__()
        self.downscale_factor = downscale_factor

    def forward(self, input):
        '''
        input: batchSize * c * k*w * k*h
        downscale_factor: k
        batchSize * c * k*w * k*h -> batchSize * k*k*c * w * h
        '''
        return pixel_unshuffle(input, self.downscale_factor)

class RECONS_VIDEO(nn.Module):

    def __init__(self, in_channels=3, n_sequence=3, out_channels=3, n_resblock=3, n_feat=32,
                 kernel_size=5, extra_channels=0, feat_in=False, n_in_feat=1, need_event=False):
        super(RECONS_VIDEO, self).__init__()
        print("Creating Recons-Video Net")

        self.feat_in = feat_in

        if not extra_channels == 0:
            print("SRN Video Net extra in channels: {}".format(extra_channels))

        InBlock = []
        if not feat_in:
            InBlock.extend([nn.Sequential(
                nn.Conv2d(in_channels * n_sequence + extra_channels, n_feat, kernel_size=kernel_size, stride=1,
                          padding=kernel_size // 2),
                nn.LeakyReLU(0.2, inplace=True)
            )])
            print("The input of SRN is image")
        else:
            InBlock.extend([nn.Sequential(
                nn.Conv2d(n_in_feat, n_feat, kernel_size=kernel_size, stride=1, padding=kernel_size // 2),
                nn.LeakyReLU(0.2, inplace=True)
            )])
            print("The input of SRN is feature")
        InBlock.extend([blocks.ResBlock(n_feat, n_feat, kernel_size=kernel_size, stride=1)
                        for _ in range(n_resblock)])

        # encoder1
        Encoder_first = [nn.Sequential(
            nn.Conv2d(n_feat, n_feat * 2, kernel_size=kernel_size, stride=2, padding=kernel_size // 2),
            nn.LeakyReLU(0.2, inplace=True)
        )]
        Encoder_first.extend([blocks.ResBlock(n_feat * 2, n_feat * 2, kernel_size=kernel_size, stride=1)
                              for _ in range(n_resblock)])
        # encoder2
        Encoder_second = [nn.Sequential(
            nn.Conv2d(n_feat * 2, n_feat * 4, kernel_size=kernel_size, stride=2, padding=kernel_size // 2),
            nn.LeakyReLU(0.2, inplace=True)
        )]
        Encoder_second.extend([blocks.ResBlock(n_feat * 4, n_feat * 4, kernel_size=kernel_size, stride=1)
                               for _ in range(n_resblock)])

        #LSTM
        self.midLSTM = blocks.ConvLSTM(n_feat * 4, n_feat * 4, 3, 1)

        # decoder2
        Decoder_second = [blocks.ResBlock(n_feat * 4, n_feat * 4, kernel_size=kernel_size, stride=1)
                          for _ in range(n_resblock)]
        Decoder_second.append(nn.Sequential(
            nn.ConvTranspose2d(n_feat * 4, n_feat * 2, kernel_size=3, stride=2, padding=1, output_padding=1),
            nn.LeakyReLU(0.2, inplace=True)
        ))
        # decoder1
        Decoder_first = [blocks.ResBlock(n_feat * 2, n_feat * 2, kernel_size=kernel_size, stride=1)
                         for _ in range(n_resblock)]
        Decoder_first.append(nn.Sequential(
            nn.ConvTranspose2d(n_feat * 2, n_feat, kernel_size=3, stride=2, padding=1, output_padding=1),
            nn.LeakyReLU(0.2, inplace=True)
        ))

        OutBlock = [blocks.ResBlock(n_feat, n_feat, kernel_size=kernel_size, stride=1)
                    for _ in range(n_resblock)]
        OutBlock.append(
            nn.Conv2d(n_feat, out_channels, kernel_size=kernel_size, stride=1, padding=kernel_size // 2)
        )
        if need_event:
            self.event_in = nn.Sequential(
                nn.Conv2d(in_channels=40, out_channels=n_feat, kernel_size=3, stride=1, padding=1, bias=False),
                nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(in_channels=n_feat, out_channels=n_feat, kernel_size=kernel_size, stride=2,
                          padding=kernel_size // 2, bias=False),
                nn.LeakyReLU(0.2, inplace=True),
                nn.Conv2d(in_channels=n_feat, out_channels=n_feat, kernel_size=3, stride=1, padding=1, bias=False)
            )
            self.PS = Pixel_UnShuffle(2)

        self.inBlock = nn.Sequential(*InBlock)
        self.encoder_first = nn.Sequential(*Encoder_first)
        self.encoder_second = nn.Sequential(*Encoder_second)
        self.decoder_second = nn.Sequential(*Decoder_second)
        self.decoder_first = nn.Sequential(*Decoder_first)
        self.outBlock = nn.Sequential(*OutBlock)
        self.transform1 = blocks.NONLocalBlock2D(n_feat*4, n_feat)

    def forward(self, x, state=None, event=None):
        if x.ndimension() == 5:
            b, n, c, h, w = x.size()
            frame_list = [x[:, i, :, :, :] for i in range(n)]
            x = torch.cat(frame_list, dim=1)
        # x = torch.cat((x, event), dim=1)
        # encoder
        first_scale_inblock = self.inBlock(x)
        first_scale_encoder_first = self.encoder_first(first_scale_inblock)
        first_scale_encoder_second = self.encoder_second(first_scale_encoder_first)

        if event is not None:
            feature_eve = self.event_in(event)
            eve_feature = self.PS(feature_eve)
        else:
            eve_feature = first_scale_encoder_second

        first_scale_encoder_second = self.transform1(first_scale_encoder_second, eve_feature)
        if state is None:
            hidden = first_scale_encoder_second
        else:
            hidden, state = self.midLSTM(first_scale_encoder_second, state)

        first_scale_decoder_second = self.decoder_second(hidden)

        first_scale_decoder_first = self.decoder_first(first_scale_decoder_second + first_scale_encoder_first)

        first_scale_outBlock = self.outBlock(first_scale_decoder_first + first_scale_inblock)

        return first_scale_outBlock, state, None, None  # first_scale_decoder_first_eve
